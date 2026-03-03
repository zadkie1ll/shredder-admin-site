import uuid
from django.db import models
from django.conf import settings
from django.utils import timezone

class MagicToken(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    token = models.UUIDField(default=uuid.uuid4, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_used = models.BooleanField(default=False)

    def is_valid(self):
        # Токен живет 15 минут
        expiration_time = self.created_at + timezone.timedelta(minutes=15)
        return timezone.now() < expiration_time and not self.is_used