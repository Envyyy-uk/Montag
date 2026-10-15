"""Фейковий бекенд Gemini для тестів: ні мережі, ні грошей."""

import json
import re

from analysis.gemini_client import BatchStatus, Result, UploadedFile


def good_answer(contents, score=7.5, tags=("натовп",)):
    prompt = contents[0]["parts"][0]["text"]
    ids = [int(m) for m in re.findall(r"^(\d+): ", prompt, re.M)]
    return json.dumps({"event_type": "концерт", "event_confidence": 0.9, "segments": [
        {"id": i, "score": score, "description": "натовп стрибає", "tags": list(tags),
         "vertical_ok": True, "subject_x": 0.5} for i in ids]}, ensure_ascii=False)


USAGE = {"prompt_token_count": 20000, "candidates_token_count": 400, "thoughts_token_count": 100,
         "prompt_tokens_details": [{"modality": "VIDEO", "token_count": 12000},
                                   {"modality": "AUDIO", "token_count": 5000},
                                   {"modality": "TEXT", "token_count": 3000}]}


class FakeBackend:
    def __init__(self, answer=good_answer, polls_until_done=1, batch_state="JOB_STATE_SUCCEEDED"):
        self.answer = answer
        self.polls_until_done = polls_until_done
        self.batch_state = batch_state
        self.uploads = []
        self.batches = {}
        self.polls = 0

    def upload(self, path, mime_type):
        self.uploads.append((path.name, mime_type))
        return UploadedFile(uri=f"https://fake/{path.name}", name=f"files/{len(self.uploads)}",
                            expires_at="2999-01-01T00:00:00+00:00")

    def submit_batch(self, model, requests, display_name):
        name = f"batches/{len(self.batches) + 1}"
        self.batches[name] = requests
        return name

    def get_batch(self, name):
        self.polls += 1
        if self.polls < self.polls_until_done:
            return BatchStatus(state="JOB_STATE_RUNNING")
        if self.batch_state != "JOB_STATE_SUCCEEDED":
            return BatchStatus(state=self.batch_state, error="boom")
        results = {}
        for r in self.batches[name]:
            text = self.answer(r["contents"])
            results[r["key"]] = Result(text=text, usage=USAGE, model_version="gemini-3.6-flash")
        return BatchStatus(state="JOB_STATE_SUCCEEDED", results=results)

    def generate(self, model, contents, config):
        return Result(text=self.answer(contents), usage=USAGE, model_version="gemini-3.6-flash")

    @property
    def requests(self):
        return [r for reqs in self.batches.values() for r in reqs]
