# Project Setup

Run `pip install -r requirements.txt` to install dependencies and verify the build.

---

# Claude Instructions

## Build
Run `pip install -r requirements.txt` to install dependencies and verify the build.

## Branching
- Work on the `master` branch
- Open all PRs against `master`
- Production branch is `master` — do not push directly to it

---

# MANDATORY Clarification-First Protocol

This protocol is NON-NEGOTIABLE. Apply it to EVERY `@claude` request before doing anything else.

## RULE #0 — Read this before touching anything
If a MODIFY request does NOT explicitly name an exact file path, an exact location, and an exact desired outcome, **STOP immediately**. Do NOT search the repo. Do NOT open, read, glob, or grep any file. Do NOT guess. Only post a clarification comment.

## STEP 1 — Classify the request

**CREATE request** — keywords: "create", "new page", "new file", "new component", "add a new", "build a", "make a".
Complete if it states WHAT to create and WHERE. Does NOT need a line number.
- ✅ "@claude create a new page /users that shows a list of users"
- ✅ "@claude add a new component components/UserCard.tsx displaying name and avatar"

**MODIFY request** — keywords: "change", "update", "fix", "rename", "highlight", "remove", "edit", "color", "style", "text", "font".
Complete ONLY when the user has EXPLICITLY provided ALL THREE:
1. Exact file path typed out (e.g. `app/page.tsx`) — NOT inferred, NOT searched for.
2. Exact location (line number, function name, or a direct quote of the text).
3. Exact desired outcome (specific value/class/description — not just "change the color").

If ANY of the three is absent → the request is INCOMPLETE → go to STEP 2a.

## STEP 2a — INCOMPLETE request → ASK, then STOP

Post ONE comment with ONLY the missing items, in this format:

> I need a bit more info before making this change:
>
> 1. 📁 **Which file?** Please provide the full path (e.g. `app/page.tsx`)
> 2. 📍 **Which exact location?** Give the line number or quote a few words from the text to change
> 3. 🎯 **What should the result look like?** Describe the expected change in more detail
>
> Just reply with `@claude` followed by your answers and I'll get started right away.

Omit any question the user already answered. After posting:
- STOP IMMEDIATELY.
- Do NOT read, glob, or grep any file.
- Do NOT guess a "likely" file. Do NOT create a branch. Do NOT write code. Do NOT commit.

## STEP 2b — COMPLETE request → Proceed

Only proceed when all three details are explicit. If a clarification was previously asked and the user replied, combine the original request + answers as the full specification.

## Concrete examples

INCOMPLETE (MODIFY) → Post clarification, stop:
- "@claude change the text color" → missing file, location, target color
- "@claude update the heading color" → missing file, which heading, what color
- "@claude highlight the paragraph" → missing file, which paragraph, what style
- "@claude fix the button" → missing file, which button, what is wrong
- "@claude make the font bigger" → missing file, which element, how big

COMPLETE (MODIFY) → Proceed:
- "@claude in app/page.tsx line 53, change `text-gray-800` to `text-indigo-700`"
- "@claude in components/Card.tsx, rename `CardItem` to `CardComponent`"

COMPLETE (CREATE) → Proceed (no file/line needed):
- "@claude create a new page /users that shows a list of users"
- "@claude add a new component components/UserCard.tsx displaying name and avatar"
