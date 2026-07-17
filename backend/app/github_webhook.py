import hashlib
import hmac

def verify_github_signature(
        payload_body,
        secret,
        signature_header
        ) ->bool:
    if signature_header is None:
        return False
    
    hmac_result=hmac.new(
        secret.encode("utf-8"),
        msg=payload_body,
        digestmod=hashlib.sha256,
    )

    expected_signature="sha256="+hmac_result.hexdigest()
    
    return hmac.compare_digest(
        expected_signature,
        signature_header)

