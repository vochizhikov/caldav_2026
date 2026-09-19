from aiogram.fsm.state import State, StatesGroup


class ConnectAccount(StatesGroup):
    login = State()
    password = State()


class EditSettings(StatesGroup):
    timezone = State()
