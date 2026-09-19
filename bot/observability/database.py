"""Removable, transaction-aware SQL audit for one async session factory.

SQL statements describe the operations sent to the database. They are not row
snapshots: server-side defaults, triggers and foreign-key cascades are not
expanded into invented changes. No additional database queries are performed.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from uuid import uuid4
from weakref import WeakSet, ref

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

MAX_STATEMENTS = 100
MAX_TRANSACTION_BYTES = 256_000
MAX_PARAMETER_ROWS = 25

# Literal SQL values are excluded even when callers use text() instead of binds.
_SQL_LITERALS = re.compile(r"\$(\w*)\$.*?\$\1\$|'(?:''|\\.|[^'\\])*'", re.DOTALL)


@dataclass
class _Transaction:
    transaction_id: str = field(default_factory=lambda: uuid4().hex[:16])
    statements: list = field(default_factory=list)
    size: int = 0
    omitted_statements: int = 0
    committed: bool = False

    def append(self, statement):
        size = len(json.dumps(statement, ensure_ascii=False, default=str).encode("utf-8"))
        if len(self.statements) >= MAX_STATEMENTS or self.size + size > MAX_TRANSACTION_BYTES:
            self.omitted_statements += 1
        else:
            self.statements.append(statement)
            self.size += size


def install_database_audit(
    engine: AsyncEngine, sessions: async_sessionmaker, audit
) -> Callable[[], None]:
    """Attach scoped listeners; call the returned function to detach them.

    Install before creating sessions and detach after active sessions finish.
    Writes are emitted only when the outer transaction ends. A released
    savepoint is not reported as a committed database transaction.
    """
    original_class = sessions.kw.get("sync_session_class")
    base_class = original_class or sessions.class_.sync_session_class

    class AuditedSession(base_class):
        pass

    key = f"database_audit_{uuid4().hex}"
    tracked_sessions = WeakSet()
    listeners = []

    def listen(target, name, callback):
        @wraps(callback)
        def guarded(*args, **kwargs):
            try:
                return callback(*args, **kwargs)
            except Exception as error:
                # Observability must never make an application transaction fail.
                audit.emit("database", "capture_failed", error_type=type(error).__name__)
                return None

        event.listen(target, name, guarded)
        listeners.append((target, name, guarded))

    def current_transaction(session):
        return session.get_nested_transaction() or session.get_transaction()

    def after_transaction_create(session, transaction):
        if transaction.parent is None or transaction.nested:
            tracked_sessions.add(session)
            state = session.info.setdefault(key, {"transactions": {}, "connections": []})
            state["transactions"][transaction] = _Transaction()

    def after_begin(session, transaction, connection):
        connection.info[key] = ref(session)
        # Keep the info dictionary: Connection.info cannot be read after close.
        session.info[key]["connections"].append(connection.info)

    def session_for(connection):
        session_ref = connection.info.get(key)
        session = session_ref() if session_ref else None
        if session is None or key not in session.info:
            return None
        return session

    def statement_data(statement, parameters, context, executemany, rowcount):
        compiled = getattr(context, "compiled", None)
        sql = _SQL_LITERALS.sub("'[literal omitted]'", statement)
        operation = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else "UNKNOWN"
        if getattr(context, "isinsert", False):
            operation = "INSERT"
        elif getattr(context, "isupdate", False):
            operation = "UPDATE"
        elif getattr(context, "isdelete", False):
            operation = "DELETE"
        if operation not in {"INSERT", "UPDATE", "DELETE", "SELECT", "WITH"}:
            return None
        if operation == "SELECT" and audit.mode != "detailed":
            return None

        # Compiler parameter names preserve the secret-field names for the
        # sanitizer, including SQLite/asyncpg's positional DBAPI placeholders.
        compiled_parameters = getattr(context, "compiled_parameters", None)
        values = compiled_parameters if compiled_parameters is not None else parameters
        count = len(values) if isinstance(values, (tuple, list)) else 1
        if isinstance(values, (tuple, list)):
            values = values[:MAX_PARAMETER_ROWS]
        if compiled is None and not isinstance(values, dict):
            values = "[unlabelled raw SQL parameters omitted]"
        return audit.sanitize(
            {
                "operation": operation,
                "sql": sql[:6000],
                "parameters": values,
                "parameter_sets": count,
                "omitted_parameter_sets": max(0, count - MAX_PARAMETER_ROWS),
                "rowcount": rowcount if rowcount is not None and rowcount >= 0 else None,
                "executemany": bool(executemany),
            }
        )

    def after_cursor_execute(connection, cursor, statement, parameters, context, executemany):
        session = session_for(connection)
        if session is None:
            return
        data = statement_data(statement, parameters, context, executemany, cursor.rowcount)
        if data is not None:
            transaction = current_transaction(session)
            session.info[key]["transactions"][transaction].append(data)

    def handle_error(context):
        if context.connection is None:
            return
        session = session_for(context.connection)
        if session is None:
            return
        transaction = session.info[key]["transactions"].get(current_transaction(session))
        data = statement_data(
            context.statement or "", context.parameters, context.execution_context, False, None
        )
        audit.emit(
            "database",
            "statement_failed",
            transaction_id=transaction.transaction_id if transaction else None,
            statement=data,
            # Exception messages often repeat SQL parameters, including secrets.
            error_type=type(context.original_exception).__name__,
        )

    def after_commit(session):
        transaction = (
            session.info.get(key, {}).get("transactions", {}).get(current_transaction(session))
        )
        if transaction is not None:
            transaction.committed = True

    def after_transaction_end(session, transaction):
        state = session.info.get(key)
        if state is None:
            return
        recorded = state["transactions"].pop(transaction, None)
        if recorded is None:
            return  # SQLAlchemy's internal flush subtransaction.
        parent = transaction.parent
        while parent is not None and parent not in state["transactions"]:
            parent = parent.parent
        parent_record = state["transactions"].get(parent)
        if recorded.committed and parent_record is not None:
            for statement in recorded.statements:
                parent_record.append(statement)
            parent_record.omitted_statements += recorded.omitted_statements
        elif recorded.statements or recorded.omitted_statements:
            status = "committed" if recorded.committed else "rolled_back"
            audit.emit(
                "database",
                f"transaction_{status}",
                transaction_id=recorded.transaction_id,
                parent_transaction_id=parent_record.transaction_id if parent_record else None,
                status=status,
                statements=recorded.statements,
                omitted_statements=recorded.omitted_statements,
            )
        if transaction.parent is None:
            for info in state["connections"]:
                owner = info.get(key)
                if owner is not None and owner() is session:
                    info.pop(key, None)
            session.info.pop(key, None)

    listen(AuditedSession, "after_transaction_create", after_transaction_create)
    listen(AuditedSession, "after_begin", after_begin)
    listen(AuditedSession, "after_commit", after_commit)
    listen(AuditedSession, "after_transaction_end", after_transaction_end)
    listen(engine.sync_engine, "after_cursor_execute", after_cursor_execute)
    listen(engine.sync_engine, "handle_error", handle_error)
    sessions.configure(sync_session_class=AuditedSession)

    def cleanup():
        for target, name, callback in listeners:
            event.remove(target, name, callback)
        listeners.clear()
        for session in tracked_sessions:
            state = session.info.pop(key, None)
            if state is not None:
                for info in state["connections"]:
                    info.pop(key, None)
        if sessions.kw.get("sync_session_class") is AuditedSession:
            sessions.configure(sync_session_class=original_class)

    return cleanup
