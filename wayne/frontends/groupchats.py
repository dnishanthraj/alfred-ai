"""
Group chats on the console: the part that runs them in time.

Mixed into Console (wayne.frontends.web), which supplies the broadcast, the
turn lock, presence-paced reading (`_wait_to_read`), sessions and the budget for
unprompted messages. What a group *is* lives in wayne.memory.groups; how one
behaves — who knows what, who answers, when it goes quiet — in
wayne.engine.groupchat. This is only the clock: each member reads in their own
time, decides, types, and posts, and whatever they post is read by the others
in turn.
"""
import asyncio
import logging
import random
import time

from ..engine import groupchat, initiative, presence
from ..memory import groups as store

log = logging.getLogger("wayne")

# Started typing, thought better of it: how often a decision to say nothing shows.
FALSE_START = 0.12
# A question he put to someone who hasn't read it: how long before anyone
# else says something (seconds), and the chance per pulse once they might.
CHASE_AFTER = (6 * 60, 50 * 60)
CHASE_ODDS = 0.04
# After a group call, the chance it carries on in a group they share.
AFTER_CALL = 0.3


class GroupChats:
    # Seconds of quiet from Bruce before a member reading along answers him.
    GROUP_SETTLE = 9

    def _init_groups(self):
        # One reader per member per group: reading, deciding, perhaps replying.
        self._group_readers = {}

    # --- what the page asks for ---------------------------------------------

    def group_payload(self, group):
        summary = group.summary()
        last = summary["last"]
        if last:
            summary["last"] = {**last, "read_by": group.readers_of(last)}
        return summary

    async def group_create(self, name, members):
        members = [m for m in dict.fromkeys(members or []) if self.directory.get(m)]
        if len(members) < 2:
            return None
        group = store.create(name or ", ".join(self.directory.get(m).name for m in members), members)
        await self.broadcast({"type": "group_created", "group": self.group_payload(group)})
        return group

    async def group_delete(self, group_id):
        group = store.of(group_id)
        if group is None:
            return
        for key, task in list(self._group_readers.items()):
            if key[0] == group_id:
                task.cancel()
                self._group_readers.pop(key, None)
        group.delete()
        await self.broadcast({"type": "group_deleted", "id": group_id})

    async def group_text(self, group_id, body):
        """Bruce posts. Everyone in it will read it — when they get to it."""
        group, body = store.of(group_id), (body or "").strip()
        if group is None or not body:
            return
        message = group.add("me", body)
        groupchat.spend(group, "me")
        await self.broadcast({"type": "group_sent", "group": group_id, "message": message})
        await self._ping(group, body, "me")
        for member in group.members:
            self._read_later(group, member)

    async def _ping(self, group, body, sender):
        """
        Named in a message, the phone lights up with their name: likelier to
        look now — and tagged with an @, it pings like a message of their own.
        Whoever sent it, him or one of them.
        """
        for member in group.members:
            contact = self.directory.get(member)
            if contact is None or member == sender or not groupchat.addressed(body, contact):
                continue
            whereabouts = presence.of(contact)
            pull = 3.0 if groupchat.tagged(body, contact) else 1.5
            if (whereabouts.now()["status"] != presence.ONLINE
                    and random.random() < (contact.texting_pace or {}).get("wake", 0.2) * pull):
                whereabouts.touch()
                await self._presence_changed(contact)

    async def group_react(self, group_id, message_id, emoji):
        """He taps a reaction on a message. They see it when they next read; nobody's owed a reply."""
        group = store.of(group_id)
        if group is None:
            return
        message = group.react(message_id, "me", (emoji or "")[:16] or None)
        if message:
            await self.broadcast({"type": "group_reaction", "group": group_id, "message": message})
            author = self.directory.get(message.get("from"))
            if emoji and author is not None and random.random() < initiative.tapback_odds(author, emoji) * 0.7:
                self._spawn(self._answer_group_tapback(group_id, author, message, emoji))

    async def _answer_group_tapback(self, group_id, contact, message, emoji):
        """Now and then, whoever wrote it says something about his reaction — once they've seen it."""
        delay = presence.read_delay(contact, presence.of(contact).now())
        if delay is None:
            return
        await asyncio.sleep(delay + random.uniform(3, 20))
        group = store.of(group_id)
        if group is None or contact.id not in group.members:
            return
        latest = [m for m in group.messages() if m.get("kind") != "system"][-3:]
        mine = next((m for m in group.messages() if m.get("id") == message.get("id")), None)
        if mine is None or (mine.get("reactions") or {}).get("me") != emoji or mine not in latest:
            return      # he took it back, or the chat's moved on
        await self._post_as(group, contact, tapback=f"{emoji} to your message “{message['text'][:80]}”")

    # --- each member, in their own time -------------------------------------

    def _read_later(self, group, member):
        key = (group.id, member)
        contact = self.directory.get(member)
        if contact is None or key in self._group_readers:
            return
        self._group_readers[key] = self._spawn(self._read_group(group.id, contact))

    async def _read_group(self, group_id, contact):
        key = (group_id, contact.id)
        try:
            await self._wait_to_read(contact)
            group = store.of(group_id)
            if group is None or contact.id not in group.members:
                return
            # He's still sending — "Dick." / "Convince Randy to..." / "Come home." —
            # so wait for him to finish, as anyone reading along would, rather
            # than answer each fragment.
            while True:
                last = (group.messages() or [{}])[-1]
                if last.get("from") != "me" or time.time() - last.get("at", 0) > self.GROUP_SETTLE:
                    break
                await asyncio.sleep(3)
            unread = group.unread_for(contact.id)
            if not unread:
                return
            read_at = group.mark_read(contact.id)
            await self.broadcast({"type": "group_read", "group": group_id, "member": contact.id,
                                  "at": read_at})
            await self._maybe_reply(group, contact, unread)
            self._maybe_aside(group, contact, unread)
        finally:
            self._group_readers.pop(key, None)
            # More arrived while they were reading or writing: another look.
            group = store.of(group_id)
            if group is not None and group.unread_for(contact.id):
                self._read_later(group, contact.id)

    async def _maybe_reply(self, group, contact, unread):
        # Owed an answer when he asks; one of them asking is likely, not owed.
        must = any(groupchat.addressed(m["text"], contact) for m in unread if m["from"] == "me")
        lively = groupchat.liveliness(group, self.directory)
        if not must and random.random() >= groupchat.reply_odds(contact, group, unread,
                                                                groupchat.energy(group), lively):
            return
        await asyncio.sleep(random.uniform(0.8, 4.0))
        await self._post_as(group, contact, unread=unread, must=must)

    def _maybe_aside(self, group, contact, unread):
        """
        Now and then, having read the group, someone messages him privately —
        about what someone said, or what they wouldn't say in front of the others.
        Rare, and counted against the same budget as any unprompted text.
        """
        bruce_spoke = any(m["from"] == "me" for m in unread)
        if random.random() >= (0.08 if bruce_spoke else 0.03) or not self._may_reach_out(time.time()):
            return
        self._count_initiative()
        about = (f"something from the group chat “{group.name}” you'd rather say to him privately — "
                 "about what someone said there, how he came across, or what you wouldn't say in "
                 "front of the others")
        self._spawn(self._later(random.uniform(40, 360), self._send_unprompted(contact, about, "impulse")))

    @staticmethod
    async def _later(delay, coroutine):
        await asyncio.sleep(delay)
        await coroutine

    async def carry_out_group_task(self, contact, chat_name, task):
        """
        He asked them in a DM to say or do something in a group. They find the
        chat they're both in that he meant, and go and do it — in a moment, in
        their own words. Asked about a chat that isn't clear, they'll have asked
        him which in the DM, and nothing happens here.
        """
        import difflib
        theirs = store.groups_with(contact.id)
        names = {g.name.lower().strip(" .!"): g for g in theirs}
        match = difflib.get_close_matches(chat_name.lower().strip(" .!"), list(names), 1, 0.5)
        group = names[match[0]] if match else (theirs[0] if len(theirs) == 1 else None)
        if group is None:
            log.info("%s couldn't tell which group was meant", contact.id)
            return
        await asyncio.sleep(random.uniform(15, 75))
        log.info("%s doing what he asked in group %s", contact.id, group.id)
        await self._post_as(group, contact, task=task)

    async def _post_as(self, group, contact, unread=(), must=False, opening=None, task=None, chase=None,
                       tapback=None, given=None):
        """
        They write, type and send — then everyone else reads it in turn.
        `given` is something they've already written for the group (in the
        wrong thread): it's sent as it is.
        """
        session = self.session_for(contact.id)
        loop = asyncio.get_running_loop()
        session._group_actions = {"leave": False, "add": []}
        if given is not None:
            text, unread = given, list(unread)
        else:
            async with self.turn_lock:
                # Anything said while they waited their turn to write, they've seen
                # too — two people answering the same question without either
                # noticing the other was the rule, not the exception.
                unread = list(unread)
                seen = {m.get("id") for m in unread}
                fresh = [m for m in group.messages() if m["at"] > group.read_upto(contact.id)
                         and m["from"] != contact.id and m.get("id") not in seen]
                if fresh:
                    unread += fresh
                    read_at = group.mark_read(contact.id)
                    await self.broadcast({"type": "group_read", "group": group.id, "member": contact.id,
                                          "at": read_at})
                text = await loop.run_in_executor(None, session.group_post, group, unread, must,
                                                  opening, task, chase, tapback)
        actions = getattr(session, "_group_actions", {}) or {}
        if task:
            actions["asked"] = True     # he asked for this, in a DM: it stands
        if actions.get("react"):
            await self._tapback(group, contact, unread, actions["react"])
        if not text:
            if (not actions.get("react") and random.random() < FALSE_START
                    and presence.of(contact).now()["status"] == presence.ONLINE):
                # Started to say something, thought better of it.
                await self.broadcast({"type": "group_typing", "group": group.id, "speaker": contact.id})
                await asyncio.sleep(random.uniform(2.5, 7))
                await self.broadcast({"type": "group_idle", "group": group.id, "speaker": contact.id})
            await self._group_actions(group, contact, actions)
            return
        started = loop.time()
        per_second = max(1.0, (contact.texting_pace or {}).get("wpm", 50) * 5 / 60)
        mine = [m["text"] for m in group.messages()[-20:] if m["from"] == contact.id][-3:]
        text = groupchat.fix_tags(text, group, self.directory, contact.id)
        text = groupchat.needless_tags(text, group, self.directory, contact.id)
        if not text:
            await self._group_actions(group, contact, actions)
            return
        parts = initiative.bubbles(contact, initiative.untic(contact, text, mine))
        for i, part in enumerate(parts):
            current = store.of(group.id)
            if current is None or contact.id not in current.members:
                # Removed while writing: it doesn't get sent.
                await self.broadcast({"type": "group_idle", "group": group.id, "speaker": contact.id})
                return
            await self.broadcast({"type": "group_typing", "group": group.id, "speaker": contact.id})
            await self._type_out(contact.id, part, per_second, group=group.id, cap=15,
                                 already=(loop.time() - started) if i == 0 else 0)
            message = group.add(contact.id, part)
            await self.broadcast({"type": "group_message", "group": group.id, "message": message})
            await self._ping(group, part, contact.id)
            if i < len(parts) - 1:
                await asyncio.sleep(random.uniform(0.4, 1.2))
        presence.of(contact).touch()
        await self._presence_changed(contact)
        log.info("%s posted in group %s, %d messages", contact.id, group.id, len(parts))
        groupchat.spend(group, contact.id)
        await self._group_actions(group, contact, actions)
        for member in group.members:
            if member != contact.id:
                self._read_later(group, member)

    async def _dm_from_group(self, contact, text):
        from ..engine.session import REACH_MARKER
        await asyncio.sleep(random.uniform(4, 20))
        self.session_for(contact.id).history.record_exchange(REACH_MARKER, text, via="text")
        await self._deliver(contact, text, asyncio.get_running_loop().time(), origin="aside")

    async def _tapback(self, group, contact, unread, emoji):
        """Their reaction lands on the latest thing someone else said."""
        target = next((m for m in reversed(list(unread) or group.messages()[-6:])
                       if m.get("kind") != "system" and m["from"] != contact.id and m.get("id")), None)
        if target is None or (target.get("reactions") or {}).get(contact.id) == emoji:
            return
        await asyncio.sleep(random.uniform(1, 6))
        message = group.react(target["id"], contact.id, emoji)
        if message:
            log.info("%s reacted %s in group %s", contact.id, emoji, group.id)
            await self.broadcast({"type": "group_reaction", "group": group.id, "message": message})

    async def _group_actions(self, group, contact, actions):
        """They add someone to the group, or walk out of it — their call, in context."""
        if actions.get("dm"):
            # Taken private: a text to him, in their thread, as they'd send it.
            self._spawn(self._dm_from_group(contact, actions["dm"]))
        for name in actions.get("add", [])[:1]:
            newcomer = next((c for c in self.directory
                             if name.lower() in (c.name.lower(), c.full_name.lower(), c.id)), None)
            if (newcomer and newcomer.id != contact.id and newcomer.id not in group.members
                    and (actions.get("asked") or groupchat.may_add(group, contact, newcomer))
                    and group.add_member(newcomer.id)):
                groupchat.note_added(group)
                line = group.system(f"{contact.name} added {newcomer.name}")
                log.info("%s added %s to group %s", contact.id, newcomer.id, group.id)
                await self.broadcast({"type": "group_message", "group": group.id, "message": line})
                await self.broadcast({"type": "group_updated", "group": self.group_payload(group)})
                self._read_later(group, newcomer.id)
        for name in actions.get("remove", []):
            member = next((c for c in self.directory
                           if name.lower() in (c.name.lower(), c.full_name.lower(), c.id)), None)
            if (member and member.id != contact.id and member.id in group.members
                    and len(group.members) > 1
                    and (actions.get("asked") or groupchat.may_remove(group, contact, member))
                    and group.remove_member(member.id)):
                await self._drop_member(group, member.id)
                line = group.system(f"{contact.name} removed {member.name}")
                log.info("%s removed %s from group %s", contact.id, member.id, group.id)
                await self.broadcast({"type": "group_message", "group": group.id, "message": line})
                await self.broadcast({"type": "group_updated", "group": self.group_payload(group)})
        if actions.get("leave") and contact.id in group.members and len(group.members) > 1:
            group.remove_member(contact.id)
            line = group.system(f"{contact.name} left")
            log.info("%s left group %s", contact.id, group.id)
            await self.broadcast({"type": "group_message", "group": group.id, "message": line})
            await self.broadcast({"type": "group_updated", "group": self.group_payload(group)})
            self._others_read(group, contact.id)

    def _others_read(self, group, but=None):
        """After a change to the group, the rest read it — and may react."""
        for member in group.members:
            if member != but:
                self._read_later(group, member)

    async def _drop_member(self, group, contact_id):
        """Out of the group: whatever they were reading or writing for it stops now."""
        task = self._group_readers.pop((group.id, contact_id), None)
        if task:
            task.cancel()
        await self.broadcast({"type": "group_idle", "group": group.id, "speaker": contact_id})

    @staticmethod
    def _bruce():
        from .. import operator
        return operator.name()

    async def group_update(self, group_id, name=None, add=(), remove=()):
        """Bruce renames the group, brings someone in, or takes someone out."""
        group = store.of(group_id)
        if group is None:
            return None
        if name and name.strip() and name.strip() != group.name:
            group.rename(name)
            line = group.system(f"You named the group “{group.name}”",
                                said=f"{self._bruce()} renamed the group “{group.name}”")
            await self.broadcast({"type": "group_message", "group": group.id, "message": line})
        for cid in add:
            contact = self.directory.get(cid)
            if contact and group.add_member(cid):
                line = group.system(f"You added {contact.name}", said=f"{self._bruce()} added {contact.name}")
                await self.broadcast({"type": "group_message", "group": group.id, "message": line})
                self._read_later(group, cid)
        for cid in remove:
            contact = self.directory.get(cid)
            if contact and len(group.members) > 1 and group.remove_member(cid, by_bruce=True):
                await self._drop_member(group, cid)
                line = group.system(f"You removed {contact.name}",
                                    said=f"{self._bruce()} removed {contact.name}")
                await self.broadcast({"type": "group_message", "group": group.id, "message": line})
                # Shown the door, they may well say something about it — to him.
                if random.random() < 0.5:
                    about = (f"he just removed you from the group chat “{group.name}” — react the way "
                             "you would")
                    self._spawn(self._later(random.uniform(20, 120),
                                            self._send_unprompted(contact, about, "impulse")))
        await self.broadcast({"type": "group_updated", "group": self.group_payload(group)})
        groupchat.spend(group, "me")        # his doing: the thread has its life back
        self._others_read(group)
        return group

    # --- the clock ---------------------------------------------------------------

    def _resume_group_reads(self):
        """Unread group messages from before a restart are still waiting to be read."""
        for group in store.all_groups():
            for member in group.members:
                if group.unread_for(member):
                    self._read_later(group, member)

    async def _group_tick(self, now):
        """
        A quiet group, now and then, gets a fresh conversation from someone in
        it — counted against the same daily budget as unprompted texts.
        """
        await self._group_chase(now)
        if not self._may_reach_out(now):
            return
        for group in store.all_groups():
            last = group.summary()["last"]
            if last and now - last["at"] < 6 * 3600:
                continue        # it's had something lately
            if any(key[0] == group.id for key in self._group_readers):
                continue
            awake = [self.directory.get(m) for m in group.members
                     if self.directory.get(m)
                     and presence.of(self.directory.get(m)).now()["status"] in (presence.ONLINE, presence.IDLE)]
            for contact in awake:
                if groupchat.wants_to_start(contact, self.PULSE_SECONDS):
                    about = initiative.impulse(self.session_for(contact.id)) or "whatever's on your mind"
                    self._count_initiative()
                    groupchat.refresh(group)
                    self._spawn(self._post_as(group, contact, opening=about))
                    return

    async def _group_chase(self, now):
        """
        He asked someone something in a group and they haven't even read it.
        A while on, someone who has may say so — "@Barb??", "she's at work",
        covering for them — and a tag pings. Once per question.
        """
        for group in store.all_groups():
            chased = group.meta().get("chased", [])
            pending = []
            for asked in (m for m in group.messages()[-12:] if m["from"] == "me"):
                if not CHASE_AFTER[0] < now - asked["at"] < CHASE_AFTER[1] or asked["id"] in chased:
                    continue
                missing = [c for c in (self.directory.get(m) for m in group.members)
                           if c and groupchat.addressed(asked["text"], c)
                           and group.read_upto(c.id) < asked["at"]]
                if missing:
                    pending.append((asked, missing))
            if not pending or random.random() >= CHASE_ODDS:
                continue
            asked, missing = pending[-1]
            age = now - asked["at"]
            absent = missing[0]
            chasers = [c for c in (self.directory.get(m) for m in group.members)
                       if c and c.id != absent.id and group.read_upto(c.id) >= asked["at"]
                       and presence.of(c).now()["status"] in (presence.ONLINE, presence.IDLE)
                       and not any(k == (group.id, c.id) for k in self._group_readers)]
            if not chasers:
                continue
            meta = group.meta()
            meta["chased"] = (meta.get("chased", []) + [asked["id"]])[-20:]
            group._save_meta(meta)
            state = presence.of(absent).now()
            from ..memory.history import describe_gap
            chase = {"name": absent.name, "ago": describe_gap(age),
                     "doing": state["doing"] if absent.shares_status else ""}
            chaser = random.choice(chasers)
            log.info("%s chasing %s in group %s", chaser.id, absent.id, group.id)
            self._spawn(self._post_as(group, chaser, chase=chase))
            return

    async def _after_group_call(self, ids):
        """
        A call with several of them just ended. Sometimes it carries on in the
        group they share — a remark, a joke at someone's expense, the plan again.
        """
        ids = set(ids)
        groups = [g for g in store.all_groups() if len(ids & set(g.members)) >= 2]
        if not groups or random.random() >= AFTER_CALL:
            return
        group = max(groups, key=lambda g: len(ids & set(g.members)))
        poster = self.directory.get(random.choice(sorted(ids & set(group.members))))
        await asyncio.sleep(random.uniform(60, 480))
        if poster is None or store.of(group.id) is None or poster.id in self._members():
            return
        log.info("%s posting in group %s after the call", poster.id, group.id)
        await self._post_as(group, poster, opening="the call with him and the others that just ended — "
                                                   "whatever you'd say about it afterwards, if anything")


