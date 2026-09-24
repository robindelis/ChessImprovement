# TRAINER-10 — `POST /train/recent_opening` crashes with HTTP 500 on any non-string `name`, stores unbounded text, and is not scoped to a colour (cross-colour poisoning of the trainer filter)
- **Severity:** medium
- **Type:** crash
- **File:** backend/api.py
- **Line(s):** 946-954
- **Component:** Repertoire trainer — recent-opening stored state
- **Repro script:** `audit/scripts/atk2_08_recent_opening.py`
- **Detected by:** ATTACKER-2 (TRAINER)

## Expected
A settings write should validate/coerce its input, cap its length, and be scoped to the
repertoire colour whose trainer consumes it (`get_repertoire_openings` returns it for *both*
colours).

## Actual
```python
@app.post("/api/repertoire/train/recent_opening")
def set_recent_learned_opening(payload: Dict[str, Any]):
    name = (payload.get("name") or payload.get("opening_name") or "").strip()
    eco  = (payload.get("eco") or "").strip()
    if name:
        set_setting("recent_learned_opening", json.dumps({"name": name, "eco": eco}))
```
`Dict[str, Any]` means no type validation, and `.strip()` is called on whatever came in.

Observed:

```
A) type confusion -- non-string `name`
   {"name": 123}            -> HTTP 500 Internal Server Error
   {"name": ["a"]}          -> HTTP 500 Internal Server Error
   {"name": {"a": 1}}       -> HTTP 500 Internal Server Error
   {"name": true}           -> HTTP 500 Internal Server Error
   {"opening_name": 3.5}    -> HTTP 500 Internal Server Error
   {"name": 0}              -> HTTP 200 {"status":"error","message":"Missing opening name"}

B) unbounded length / no validation against real studies
   200k-char name -> HTTP 200, stored bytes=200000
   non-existent opening -> HTTP 200 {'status':'success','recent_opening':{'name':'Totally Made Up Opening','eco':'Z99'}}

C) cross-colour poisoning: set a BLACK study while drilling WHITE
   white openings -> {"name": "Beta", "eco": "B12"}
   black openings -> {"name": "Beta", "eco": "B12"}
D) the trainer's 'most recent' filter is now pinned to a study the user never trained
   -> HTTP 404: 404
      (frontend alert() + clearOpeningFocus() fallback)
F) eco field is never validated
   stored: {"name": "Alpha", "eco": "<script>alert(1)</script>"}
   openings list eco fields: ['']
```

The setting is a single global row, so a name saved while scouting a **black** line becomes the
"🎯 Most Recent" entry of the **white** trainer dropdown; selecting it makes
`GET /train/next?color=white&opening=Beta` return 404, which the frontend handles with a
blocking `alert()` and `clearOpeningFocus()` (`repertoire_trainer.js:422-430`).

## Root cause
Unvalidated `Dict[str, Any]` payload + a string method on an untyped value + a colour-less
`settings` key + no length cap and no foreign-key check against `repertoire_studies`.

## Exploit / escalation
1. The stored `eco` is echoed back by `GET /api/repertoire/train/openings` and is *not*
   escaped anywhere; today the trainer only uses `textContent` so it is not XSS, but any
   future `innerHTML` use of `recent_opening` becomes a stored-XSS sink (the same payload
   already accepts 200 kB of markup).
2. `GET /train/openings?color=purple` returns **all** studies of both colours
   (`['Alpha', 'Beta']`) because `get_studies` only filters on an exact repertoire match —
   an unknown colour silently widens the dropdown instead of erroring.
3. The value is never cleared, so once poisoned the trainer shows a filter option
   (`🎯 <name>`) for a study that does not exist on every page load until the user changes it.
