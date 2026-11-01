"""Receive boundary. No unreviewed DAVE implementation is silently selected."""

from __future__ import annotations


class ReceiveUnavailable(RuntimeError):
    pass


class DiscordReceiver:
    def connect(self, *_args, **_kwargs):
        raise ReceiveUnavailable(
            "DAVE receive is gated pending pinned adapter review and authorized multi-speaker test"
        )


class RtpTimingDrop(ValueError):
    """One unusable packet; it must not invalidate the rest of a voice stream."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__("RTP packet outside the bounded session timeline")


class RtpTimeline:
    """Map each connection's 48kHz RTP clock to the same monotonic session clock.

    Adapter supplies packet arrival time relative to explicit session start and a
    new connection_id after reconnect/SSRC changes. Jitter/reorder is dropped;
    gaps remain gaps. Initial network delay is an explicit timing uncertainty.
    """

    def __init__(self):
        self.anchors: dict[str, tuple[int, int]] = {}

    def timestamp(self, connection_id: str, rtp: int, arrival_ms: int) -> int:
        if (
            type(rtp) is not int
            or type(arrival_ms) is not int
            or not 0 <= rtp < 2**32
            or arrival_ms < 0
        ):
            raise ValueError("invalid RTP timestamp")
        if connection_id not in self.anchors:
            if len(self.anchors) >= 64:
                raise ValueError("bounded RTP connection count exceeded")
            self.anchors[connection_id] = (rtp, arrival_ms // 20 * 20)
        first, offset = self.anchors[connection_id]
        delta = ((rtp - first + 2**31) % 2**32) - 2**31
        result = offset + delta // 960 * 20
        if result < 0:
            raise RtpTimingDrop("before_session")
        if result > arrival_ms + 1000:
            raise RtpTimingDrop("ahead_of_arrival")
        return result
