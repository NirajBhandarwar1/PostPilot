# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
- Per-task model lists (`SCOUT_`, `IDEAS_`, `OPTIONS_`, `BRIEF_`, `POST_PROVIDERS`) and `provider:model` pinning.
- Persona settings (`PERSONA_ROLE`, `PERSONA_WHO`, `PERSONA_GOAL`) and `voice.example.md`, so anyone can use it.
- CI (lint, types, tests on Linux and Windows), pyproject, LICENSE, SECURITY, CONTRIBUTING.

### Changed
- Scouting runs daily instead of every three days.
- Drafts and the voice profile now default to `./output` and `./voice.md` inside the project.
- A provider that reports no credit or an exhausted daily quota is skipped at once for 30 minutes.

### Fixed
- Replies sent from the owner's own Gmail account are now recognised.
- A failed email send retries only the email, not the whole scout.
