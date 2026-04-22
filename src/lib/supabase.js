// Supabase client — shared across the frontend.
//
// Env (set in .env.local for dev, Vercel Project Settings → Environment
// Variables for prod):
//   VITE_SUPABASE_URL
//   VITE_SUPABASE_ANON_KEY
//
// If either is missing the client is `null` and the app degrades to
// read-only mode — editing controls still render but saves are no-ops with
// a console warning. This keeps local dev workable before Supabase is set up.

import { createClient } from '@supabase/supabase-js';

const URL = import.meta.env.VITE_SUPABASE_URL;
const KEY = import.meta.env.VITE_SUPABASE_ANON_KEY;

export const supabase = (URL && KEY) ? createClient(URL, KEY, {
  auth: { persistSession: false },
}) : null;

export const isSupabaseEnabled = !!supabase;

if (!isSupabaseEnabled && typeof window !== 'undefined') {
  // Warn once per page load so local dev without creds isn't silent.
  // eslint-disable-next-line no-console
  console.warn('[supabase] VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY not set — edits disabled');
}
