import logging
import time

logger = logging.getLogger('activity')

SENSITIVE_KEY_PARTS = ('password', 'token', 'secret', 'salt')
SKIPPED_FIELDS = {'csrfmiddlewaretoken'}
SKIPPED_PATH_PREFIXES = ('/static/', '/media/', '/favicon.ico')


def redact(data):
    """Recursively mask values whose key looks like a password, token or secret."""
    if isinstance(data, dict):
        return {
            k: '***' if any(part in k.lower() for part in SENSITIVE_KEY_PARTS) else redact(v)
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [redact(v) for v in data]
    return data


class ActivityLogMiddleware:
    """Log every request with who made it, what they submitted (secrets redacted),
    where it went and any flash messages it produced, so a user's session can be
    retraced from the container logs."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith(SKIPPED_PATH_PREFIXES):
            return self.get_response(request)

        start = time.monotonic()
        response = self.get_response(request)
        elapsed_ms = (time.monotonic() - start) * 1000

        parts = [
            f'{request.method} {request.get_full_path()} -> {response.status_code} ({elapsed_ms:.0f}ms)',
            f'user={self._user(request)}',
            f'ip={self._ip(request)}',
        ]
        if response.has_header('Location'):
            parts.append(f'location={response["Location"]}')
        if request.method == 'POST':
            data = {
                k: v[0] if len(v) == 1 else v
                for k, v in request.POST.lists() if k not in SKIPPED_FIELDS
            }
            if data:
                parts.append(f'data={redact(data)}')
            if request.FILES:
                parts.append('files={%s}' % ', '.join(
                    f'{k}: {f.name} ({f.size} bytes)' for k, f in request.FILES.items()
                ))
        # Messages queued during this request; MessageMiddleware hasn't stored them yet.
        queued = getattr(getattr(request, '_messages', None), '_queued_messages', [])
        if queued:
            parts.append(f'messages={[f"{m.level_tag}: {m}" for m in queued]}')

        logger.info(' '.join(parts))
        return response

    @staticmethod
    def _user(request):
        user = getattr(request, 'user', None)
        if not user or not user.is_authenticated:
            return 'anonymous'
        return f'{user.email} (hijacked)' if getattr(user, 'is_hijacked', False) else user.email

    @staticmethod
    def _ip(request):
        forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
        return forwarded.split(',')[0].strip() if forwarded else request.META.get('REMOTE_ADDR', '')
