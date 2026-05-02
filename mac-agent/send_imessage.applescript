--
-- send_imessage.applescript
-- Fetches the day's digest summary and sends it to yourself via iMessage.
--
-- Args:
--   $1 = digest URL (e.g. https://you.github.io/casenet-tracker/digest.json)
--   $2 = recipient phone (+15555551234) or Apple ID email
--
-- The first time this runs you'll get a permission prompt to control Messages.
-- Approve it; subsequent runs are silent.
--

on run argv
    if (count of argv) < 2 then
        log "Usage: send_imessage.applescript <digest_url> <recipient>"
        return
    end if
    set digestURL to item 1 of argv
    set recipient to item 2 of argv

    -- Pull the digest. Use curl rather than `do shell script` JSON parsing
    -- since AppleScript has no native JSON support.
    set tmpFile to "/tmp/casenet-digest.json"
    do shell script "curl -fsSL --max-time 15 " & quoted form of digestURL & " -o " & tmpFile

    -- Extract the imessage_summary string with python (it's pre-installed on macOS).
    set summary to do shell script "python3 -c 'import json,sys; print(json.load(open(\"" & tmpFile & "\")).get(\"imessage_summary\",\"\"))'"

    if summary is "" then
        return
    end if

    -- Append today's flagged headlines (max 3) so the iMessage is glanceable
    -- without opening the dashboard.
    set headlines to do shell script "python3 -c 'import json; d=json.load(open(\"" & tmpFile & "\")); items=[h for h in d[\"hearings\"] if h[\"primary_flag\"] or h[\"flags\"]]; items.sort(key=lambda h: h[\"hearing\"][\"datetime_iso\"]); lines=[]; \nfor f in items[:3]:\n  h=f[\"hearing\"]; pf=f[\"primary_flag\"]; tag=f\" [{pf[\"label\"]} \" + str(pf[\"days_until\"]) + \"d]\" if pf else \"\";\n  from datetime import datetime; dt=datetime.fromisoformat(h[\"datetime_iso\"]).strftime(\"%a %-I:%M%p\");\n  lines.append(f\"  {dt} {h[\"case_number\"]} {h[\"hearing_type\"]}{tag}\")\nprint(\"\\n\".join(lines))'"

    if headlines is not "" then
        set summary to summary & return & headlines
    end if

    tell application "Messages"
        set targetService to 1st service whose service type = iMessage
        set targetBuddy to buddy recipient of targetService
        send summary to targetBuddy
    end tell
end run
