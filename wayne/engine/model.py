"""
The model, asked something outside a conversation turn: the background passes
— afterthoughts, day plans, status lines, dispatches, what they follow, what
travels on the grapevine.

One way of asking, so they all keep the model loaded as long as a turn would
(`MODEL_KEEP_ALIVE`) — each used to reset it to Ollama's few-minute default,
and the next call paid to load fifteen gigabytes again — and a failure is
logged rather than passing for a contact who chose to say nothing.
"""
import logging

import ollama

from .. import config

log = logging.getLogger("wayne")


def ask(model, messages, options=None, think=False, fmt=None, purpose="a background pass"):
    """
    The reply's text. Raises on failure (callers fall back as they always
    have), having logged it. `think` follows the profile when it declares one;
    the background passes otherwise ask without it, as they always did.
    """
    extra = {"keep_alive": config.MODEL_KEEP_ALIVE}
    if think is not None:
        extra["think"] = think
    if fmt:
        extra["format"] = fmt
    try:
        return ollama.chat(model=model, messages=messages, options=options or {}, **extra)["message"]["content"]
    except Exception as exc:
        log.warning("%s failed (%s): %s", purpose, model, str(exc)[:160])
        raise


def thinking(contact):
    """What to send as `think` for this contact's background passes."""
    return contact.think if getattr(contact, "think", None) is not None else False
