// Collaborative overlay for Top Movers industry / reason edits.
//
// Baseline data comes from src/data/{date}/screener.json (built by the
// Python pipeline). This module overlays human edits stored in Supabase so
// multiple teammates can annotate the same day without a redeploy.
//
// Table: screener_edits (date text, tk text, industry text, reason text,
// updated_at timestamptz, updated_by text) PK (date, tk). See
// supabase/migrations/0001_screener_edits.sql.

import { supabase, isSupabaseEnabled } from './supabase.js';

const TABLE = 'screener_edits';
const EDITABLE_FIELDS = new Set(['industry', 'reason']);

// Who made the edit — persisted in localStorage so it survives reloads.
// No auth yet: this is just a display name, not identity.
const EDITOR_KEY = 'screener_editor_name';
function getEditor() {
  if (typeof window === 'undefined') return 'anon';
  try {
    let v = window.localStorage.getItem(EDITOR_KEY);
    if (!v) {
      v = 'user-' + Math.random().toString(36).slice(2, 8);
      window.localStorage.setItem(EDITOR_KEY, v);
    }
    return v;
  } catch { return 'anon'; }
}

// Fetch all edits for a given trading day.
// Returns { [tk]: { industry?: string, reason?: string } } — empty map
// if Supabase isn't configured or the fetch fails.
export async function fetchEditsForDate(date) {
  if (!isSupabaseEnabled || !date) return {};
  const { data, error } = await supabase
    .from(TABLE)
    .select('tk, industry, reason')
    .eq('date', date);
  if (error) {
    // eslint-disable-next-line no-console
    console.warn('[screenerEdits] fetch failed:', error.message);
    return {};
  }
  const map = {};
  for (const row of data || []) {
    map[row.tk] = {
      industry: row.industry ?? undefined,
      reason:   row.reason   ?? undefined,
    };
  }
  return map;
}

// Upsert a single field for (date, tk). Returns true on success.
// Empty string clears the field (stored as null so the overlay no longer
// shadows the pipeline value).
export async function saveEdit(date, tk, field, value) {
  if (!isSupabaseEnabled) {
    // eslint-disable-next-line no-console
    console.warn('[screenerEdits] Supabase not configured — edit dropped');
    return false;
  }
  if (!EDITABLE_FIELDS.has(field)) {
    throw new Error(`saveEdit: unsupported field ${field}`);
  }
  const payload = {
    date,
    tk,
    [field]:      value === '' ? null : value,
    updated_at:   new Date().toISOString(),
    updated_by:   getEditor(),
  };
  const { error } = await supabase
    .from(TABLE)
    .upsert(payload, { onConflict: 'date,tk' });
  if (error) {
    // eslint-disable-next-line no-console
    console.warn('[screenerEdits] save failed:', error.message);
    return false;
  }
  return true;
}
