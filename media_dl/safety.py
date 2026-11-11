import re


def safe_log(message, secrets=()):
    for secret in secrets:
        if secret:
            message = message.replace(secret, '[redacted]')
    return re.sub(r'(?i)(cookie|ttwid|authorization)\s*[:=]\s*[^\r\n]+', r'\1=[redacted]', message)
