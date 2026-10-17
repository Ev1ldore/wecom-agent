"""Expose the requested SDK method names using the installed crypto library.

WXBizMsgCrypt3 is unavailable on PyPI. This adapter delegates all signature
and encryption operations to wechatpy; it implements no crypto algorithms.
"""

from wechatpy.enterprise.crypto import WeChatCrypto


class WXBizMsgCrypt:
    """Adapt wechatpy to the callback SDK's constructor and success tuples."""

    def __init__(self, token: str, aes_key: str, corpid: str) -> None:
        """Use the corporation ID as the receive ID checked by the SDK."""
        self._crypto = WeChatCrypto(token, aes_key, corpid)

    def VerifyURL(
        self, msg_signature: str, timestamp: str, nonce: str, echostr: str
    ) -> tuple[int, bytes]:
        """Verify and decrypt the URL challenge, preserving SDK exceptions."""
        plaintext = self._crypto.check_signature(
            msg_signature, timestamp, nonce, echostr
        )
        return 0, plaintext.encode("utf-8")

    def DecryptMsg(
        self, post_data: bytes, msg_signature: str, timestamp: str, nonce: str
    ) -> tuple[int, bytes]:
        """Verify and decrypt callback XML, preserving SDK exceptions."""
        plaintext = self._crypto.decrypt_message(
            post_data, msg_signature, timestamp, nonce
        )
        return 0, plaintext.encode("utf-8")
