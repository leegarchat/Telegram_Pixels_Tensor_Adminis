from aiogram.fsm.state import State, StatesGroup


class SubmitPost(StatesGroup):
    family = State()
    post_type = State()
    tags = State()
    content = State()
    banner = State()
    confirm = State()