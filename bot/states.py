from aiogram.fsm.state import State, StatesGroup


class SubmitPost(StatesGroup):
    family = State()
    sample = State()
    field = State()
    confirm = State()
    too_long = State()
