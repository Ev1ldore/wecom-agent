"""Manual contact catalog; never reads or impersonates a WeChat address book."""

import hashlib
from typing import Protocol

from .management import WhitelistFile, identity, whitelist
from .store import encoded


class ContactBookProtocol(Protocol):
    def add(self, name: str, conversation: str, sender: str, revision: str) -> dict:
        """Register an unchecked manual contact without granting reply permission."""
        raise NotImplementedError

    def state(self) -> dict:
        """Return explicitly sourced contacts and persisted selection."""
        raise NotImplementedError

    def select(self, ids: list[str], revision: str) -> dict:
        """Save all checkbox selections with optimistic concurrency control."""
        raise NotImplementedError


def contact_id(conversation, sender):
    return hashlib.sha256(encoded([conversation, sender]).encode()).hexdigest()[:24]


class ContactBook:
    def __init__(self, path):
        self.file = WhitelistFile(path)

    @staticmethod
    def catalog(config):
        entries = config.get("contacts", [])
        if not isinstance(entries, list) or len(entries) > 500:
            raise ValueError("contacts 必须为不超过500项的数组")
        contacts = {}
        for item in entries:
            if not isinstance(item, dict):
                raise ValueError("联系人格式错误")  # noqa: TRY004 -- Invalid local configuration is ValueError.
            c, s = identity(item.get("conversation")), identity(item.get("sender"))
            name = item.get("name", c)
            if not isinstance(name, str) or not name.strip() or len(name) > 80:
                raise ValueError("联系人名称必须为1至80字符")
            key = contact_id(c, s)
            if key in contacts:
                raise ValueError("联系人身份重复")
            contacts[key] = {
                "id": key,
                "name": name.strip(),
                "conversation": c,
                "sender": s,
                "source": "手动录入",
            }
        for c, s in whitelist(config):
            key = contact_id(c, s)
            contacts.setdefault(
                key,
                {
                    "id": key,
                    "name": c,
                    "conversation": c,
                    "sender": s,
                    "source": "现有白名单配置",
                },
            )
        if len(contacts) > 500:
            raise ValueError("联系人总数超过500项")
        return list(contacts.values())

    def state(self):
        config = self.file.read()
        selected = {contact_id(c, s) for c, s in whitelist(config)}
        return {
            "contacts": [
                {**item, "selected": item["id"] in selected}
                for item in self.catalog(config)
            ],
            "revision": self.file.revision(config),
            "mode": "offline",
            "wechat_connected": False,
            "config_name": self.file.path.name,
        }

    @staticmethod
    def persist_catalog(config, contacts):
        config["contacts"] = [
            {key: item[key] for key in ("name", "conversation", "sender")}
            for item in contacts
        ]

    def select(self, ids, revision):
        if (
            not isinstance(ids, list)
            or len(ids) > 500
            or any(not isinstance(x, str) for x in ids)
        ):
            raise ValueError("联系人选择无效")
        if len(set(ids)) != len(ids):
            raise ValueError("不能重复选择同一联系人")

        def transform(config):
            contacts = self.catalog(config)
            by_id = {x["id"]: x for x in contacts}
            if set(ids) - by_id.keys():
                raise ValueError("选择包含不存在的联系人，请刷新")
            self.persist_catalog(config, contacts)
            config["whitelist"] = [
                {key: by_id[i][key] for key in ("conversation", "sender")} for i in ids
            ]

        self.file.update(transform, revision=revision)
        return self.state()

    def add(self, name, conversation, sender, revision):
        c, s = identity(conversation), identity(sender)
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError("联系人名称必须为1至80字符")

        def transform(config):
            contacts = self.catalog(config)
            if any(x["id"] == contact_id(c, s) for x in contacts):
                raise ValueError("该会话和发送者已存在")
            if len(contacts) >= 500:
                raise ValueError("联系人最多500项")
            contacts.append({"name": name.strip(), "conversation": c, "sender": s})
            self.persist_catalog(config, contacts)

        self.file.update(transform, revision=revision)
        return self.state()
