"""Verify the SDK adapter against real encryption and signature validation."""

import base64
from xml.etree import ElementTree

import pytest
from wechatpy.enterprise.crypto import WeChatCrypto
from wechatpy.enterprise.exceptions import InvalidCorpIdException
from wechatpy.exceptions import InvalidSignatureException

from src.gateway.wecom_crypto import WXBizMsgCrypt

TOKEN = "round-trip-token"
AES_KEY = base64.b64encode(bytes(range(32))).decode().rstrip("=")
CORPID = "corp-round-trip"
TIMESTAMP = "1700000000"
NONCE = "round-trip-nonce"
PLAINTEXT = "<xml><Content><![CDATA[企业微信消息]]></Content></xml>"


def encrypted_message() -> tuple[bytes, str, str]:
    """Produce a signed envelope with the existing third-party implementation."""
    envelope = WeChatCrypto(TOKEN, AES_KEY, CORPID).encrypt_message(
        PLAINTEXT, NONCE, TIMESTAMP
    )
    root = ElementTree.fromstring(envelope)
    signature = root.findtext("MsgSignature")
    encrypted = root.findtext("Encrypt")
    assert signature is not None and encrypted is not None
    return envelope.encode(), signature, encrypted


def test_adapter_verifies_url_and_decrypts_message() -> None:
    """Keep the callback SDK's tuple and byte contracts without mocking crypto."""
    envelope, signature, encrypted = encrypted_message()
    crypt = WXBizMsgCrypt(TOKEN, AES_KEY, CORPID)

    assert crypt.VerifyURL(signature, TIMESTAMP, NONCE, encrypted) == (
        0,
        PLAINTEXT.encode(),
    )
    assert crypt.DecryptMsg(envelope, signature, TIMESTAMP, NONCE) == (
        0,
        PLAINTEXT.encode(),
    )


def test_adapter_rejects_wrong_receive_id() -> None:
    """Use corpid as ReceiveId so another corporation cannot decrypt successfully."""
    envelope, signature, encrypted = encrypted_message()
    crypt = WXBizMsgCrypt(TOKEN, AES_KEY, "another-corp")

    with pytest.raises(InvalidCorpIdException):
        crypt.VerifyURL(signature, TIMESTAMP, NONCE, encrypted)
    with pytest.raises(InvalidCorpIdException):
        crypt.DecryptMsg(envelope, signature, TIMESTAMP, NONCE)


def test_adapter_rejects_invalid_signatures() -> None:
    """Preserve library verification failures instead of emitting plaintext."""
    envelope, signature, encrypted = encrypted_message()
    crypt = WXBizMsgCrypt(TOKEN, AES_KEY, CORPID)
    bad_signature = ("0" if signature[0] != "0" else "1") + signature[1:]

    with pytest.raises(InvalidSignatureException):
        crypt.VerifyURL(bad_signature, TIMESTAMP, NONCE, encrypted)
    with pytest.raises(InvalidSignatureException):
        crypt.DecryptMsg(envelope, bad_signature, TIMESTAMP, NONCE)
