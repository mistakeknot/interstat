#!/usr/bin/env bash
# Shared session/bead context for plugin hooks
_context_write_session_id() { echo "$1" > "/tmp/${2:-interflux}-session-id" 2>/dev/null || true; }
_context_read_session_id() { cat "/tmp/${1:-interflux}-session-id" 2>/dev/null || echo ""; }
_context_set_bead_id() { echo "$1" > "/tmp/${2:-interflux}-bead-id" 2>/dev/null || true; }
_context_read_bead_id() { cat "/tmp/${1:-interflux}-bead-id" 2>/dev/null || echo ""; }
