"""Conservative local acknowledgment bound; SQL remains final confirmation authority."""


class DeliveryLedger:
    def __init__(self, session_id, *, generation, participant):
        self.session_id = session_id
        self.generation = generation
        self.participant = participant
        self.sequence = 0
        self.segments = {}
        self.invalidated = False

    def publish(self, segment_id, samples, sample_rate):
        if self.invalidated or segment_id in self.segments or not 0 < samples <= sample_rate * 30:
            raise ValueError("invalid segment")
        self.segments[segment_id] = {"samples": samples, "rate": sample_rate, "played": 0}

    def ack(self, message, *, participant):
        if (
            self.invalidated
            or participant != self.participant
            or message.get("version") != 1
            or message.get("session_id") != self.session_id
            or message.get("generation") != self.generation
            or type(message.get("sequence")) is not int
            or message["sequence"] <= self.sequence
        ):
            raise ValueError("stale or unauthorized playback acknowledgment")
        segment = self.segments.get(message.get("segment_id"))
        played = message.get("played_samples")
        if (
            segment is None
            or type(played) is not int
            or not segment["played"] <= played <= segment["samples"]
        ):
            raise ValueError("invalid playback samples")
        segment["played"] = played
        self.sequence = message["sequence"]

    @property
    def complete(self):
        return (
            bool(self.segments)
            and not self.invalidated
            and all(segment["played"] == segment["samples"] for segment in self.segments.values())
        )

    def invalidate(self):
        self.invalidated = True
        self.segments.clear()
