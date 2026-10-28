"""Independent control loop generation fence; heavy inference runs off the media loop."""

import asyncio


class VoiceController:
    def __init__(self, *, emit, speech):
        self.emit = emit
        self.speech = speech
        self.generation = 0
        self.closed = False

    async def speak(self, text, language):
        generation = self.generation
        await self.emit("thinking")
        try:
            pcm = await asyncio.to_thread(
                self.speech.synthesize,
                text,
                language,
                generation=self.speech.supervisor.generation
                if hasattr(self.speech, "supervisor")
                else generation,
            )
        except Exception:
            if generation == self.generation:
                await self.emit("error", code="speech_unavailable")
            return None
        if self.closed or generation != self.generation:
            return None
        await self.emit("speaking", pcm=pcm)
        return pcm

    async def interrupt(self):
        self.generation += 1
        self.speech.cancel()
        await self.emit("invalidate")
        await self.emit("listening")

    async def close(self):
        self.closed = True
        await self.interrupt()


class ActivityTimers:
    def __init__(self, *, started):
        self.started = started
        self.last_activity = started
        self.reprompts = 0
        self.muted = False

    def activity(self, now):
        self.last_activity = now
        self.reprompts = 0

    def tick(self, now):
        if now - self.started >= 600:
            return "end"
        if self.muted:
            return None
        idle = now - self.last_activity
        if idle >= 18:
            return "end"
        if idle >= 6 * (self.reprompts + 1) and self.reprompts < 2:
            self.reprompts += 1
            return "reprompt"
        return None
