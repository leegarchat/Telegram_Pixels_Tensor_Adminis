"""Finite-state machine (FSM) states for the user submission wizard.

The private-chat flow in `bot/handlers/user_submit.py` walks the user through:
  family  -> choose target channel/topic (family key),
  sample  -> choose a post template inside that family,
  field   -> answer each template placeholder one by one,
  confirm -> preview the rendered post and send it to moderation,
  too_long -> rendered post exceeds Telegram limits; user must restart.
"""
# aiogram FSM primitives: State is a single step, StatesGroup bundles the flow.
from aiogram.fsm.state import State, StatesGroup


class SubmitPost(StatesGroup):
    """Submission wizard states (stored in MemoryStorage, cleared on /start or send)."""

    # Waiting for the user to pick a family via the `fam:<key>` inline button.
    family = State()
    # Waiting for the user to pick a template via the `sample:<key>` inline button.
    sample = State()
    # Waiting for the answer to blocks[idx]: text/url/urls/photo/bool or Skip.
    field = State()
    # Preview shown; waiting for `submit:send` or `submit:restart` callback.
    confirm = State()
    # Rendered text is longer than Telegram allows; only `submit:restart` is accepted.
    too_long = State()
