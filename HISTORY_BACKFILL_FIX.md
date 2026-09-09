# Historical backfill guard

The history scheduler now skips Telegram service/empty rows that contain neither text nor downloadable photo/document media. These rows advance the local history cursor immediately and do not consume the configured publication interval.

This prevents a history job from getting stuck forever on channel-creation/migration/pin service events such as source message id 1.
