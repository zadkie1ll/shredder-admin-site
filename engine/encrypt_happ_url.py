from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
import base64

PK1 = """
-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCxsS7PUq1biQlVD92rf6eXKr9o
G1/SrYx3qWahZP+Jq35m4Wb/Z+mB6eBWrPzJ/zZpZLWLQorcvOKt+sLaCHyH1HLN
kti4jlaEQX6x97XgBm8GK08+lLLWquFDhWRNxsrfzJyNdpVopzBRmCJKTc8ObYyP
brv9T35a8Kd5WqjnUwIDAQAB
-----END PUBLIC KEY-----
"""


def encrypt_happ_url1(url: str) -> str:
    public_key = serialization.load_pem_public_key(PK1.encode())
    encrypted = public_key.encrypt(url.encode("utf-8"), padding.PKCS1v15())
    return f"happ://crypt/{base64.b64encode(encrypted).decode()}"
