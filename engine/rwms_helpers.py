import os
import logging
from datetime import datetime
from datetime import timezone
from datetime import timedelta
from typing import Optional
from common.rwms_client_sync import RwmsClientSync
import proto.rwmanager_pb2 as proto


def create_user(
    rwms_client: RwmsClientSync,
    username: str,
    trial_period_days: int,
    from_referrer: bool = False,
    email: str | None = None,
    telegram_id: int | None = None,
) -> Optional[proto.UserResponse]:
    if from_referrer:
        logging.info(
            f"creating subscription {username} with referral bonus, trial period {trial_period_days} days"
        )
    else:
        logging.info(
            f"creating subscription {username}, trial period {trial_period_days} days"
        )

    squads_uuids_value = os.getenv("INTERNAL_SQUADS_UUIDS")
    squads_uuids = []

    if squads_uuids_value:
        for squad_uuid in squads_uuids_value.split(","):
            squad_uuid = squad_uuid.strip()
            if squad_uuid:
                try:
                    squads_uuids.append(squad_uuid)
                except ValueError:
                    ...

    response = rwms_client.add_user(
        proto.AddUserRequest(
            username=username,
            email=email,
            telegram_id=telegram_id,
            expire_at=datetime.now(timezone.utc) + timedelta(days=trial_period_days),
            status=proto.UserStatus.ACTIVE,
            traffic_limit_strategy=proto.TrafficLimitStrategy.NO_RESET,
            active_internal_squads=[*squads_uuids],
            created_at=datetime.now(),
        )
    )

    return response
