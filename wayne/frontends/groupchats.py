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
        for member in group.members:
            contact = self.directory.get(member)
            # Named in it, the phone lights up with their name: likelier to look now.
            if contact and groupchat.addressed(body, contact):
                whereabouts = presence.of(contact)
                if (whereabouts.now()["status"] != presence.ONLINE
                        and random.random() < (contact.texting_pace or {}).get("wake", 0.2) * 1.5):
                    whereabouts.touch()
                    await self._presence_changed(contact)
            self._read_later(group, member)

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
        must = any(groupchat.addressed(m["text"], contact) for m in unread if m["from"] != contact.id)
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

    async def _post_as(self, group, contact, unread=(), must=False, opening=None, task=None):
        """They write, type and send — then everyone else reads it in turn."""
        session = self.session_for(contact.id)
        loop = asyncio.get_running_loop()
        session._group_actions = {"leave": False, "add": []}
        async with self.turn_lock:
            text = await loop.run_in_executor(None, session.group_post, group, list(unread), must,
                                              opening, task)
        actions = getattr(session, "_group_actions", {}) or {}
        if task:
            actions["asked"] = True     # he asked for this, in a DM: it stands
        if not text:
            await self._group_actions(group, contact, actions)
            return
        started = loop.time()
        per_second = max(1.0, (contact.texting_pace or {}).get("wpm", 50) * 5 / 60)
        mine = [m["text"] for m in group.messages()[-20:] if m["from"] == contact.id][-3:]
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

    async def _group_actions(self, group, contact, actions):
        """They add someone to the group, or walk out of it — their call, in context."""
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

    def _may_reach_out(self, now):
        from .. import config
        if config.INITIATIVE_PER_DAY <= 0 or self._quiet(now) or not self.clients:
            return False
        sent = [t for t in self._initiative_log() if now - t < 86400]
        return len(sent) < config.INITIATIVE_PER_DAY and not (sent and now - max(sent) < 45 * 60)

