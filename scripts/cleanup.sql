-- ============================================================
-- Forge: Full data reset — keep one showcase project
-- ============================================================
-- Run this in the Supabase SQL editor.
--
-- Strategy:
--   1. Find the showcase project you want to keep (showcased_at IS NOT NULL).
--      If there are multiple, keep the most-recently showcased one.
--   2. Delete everything else: all users, all other projects, all derived data.
--   3. The cascade rules on FK constraints handle child rows automatically.
--
-- After running, the DB will have:
--   - NO users (the showcase project is owned by a placeholder or orphan).
--   - All regular projects deleted.
--   - All sessions, containers, snapshots, env vars, etc. deleted.
--   - Only the single showcase project row survives (with its linked rows).
--
-- If you want to keep the showcase project's OWNER user as well, comment out
-- the DELETE FROM users block and adjust accordingly.
-- ============================================================

BEGIN;

-- ── Step 1: Identify the showcase project to keep ──────────────────────────
-- We keep the single most-recently showcased project.
-- Check what projects are showcased before running:
--   SELECT id, name, showcased_at FROM projects WHERE showcased_at IS NOT NULL ORDER BY showcased_at DESC;

DO $$
DECLARE
  keep_project_id UUID;
  keep_user_id    UUID;
BEGIN

  -- Pick the most recently showcased project
  SELECT id, user_id
    INTO keep_project_id, keep_user_id
    FROM projects
   WHERE showcased_at IS NOT NULL
   ORDER BY showcased_at DESC
   LIMIT 1;

  IF keep_project_id IS NULL THEN
    RAISE EXCEPTION 'No showcased project found. Set showcased_at on the project you want to keep first.';
  END IF;

  RAISE NOTICE 'Keeping project: % (user: %)', keep_project_id, keep_user_id;

  -- ── Step 2: Delete all snapshots except for the kept project ────────────
  DELETE FROM snapshots
   WHERE project_id <> keep_project_id;

  -- ── Step 3: Delete all project_env_vars except kept project ─────────────
  DELETE FROM project_env_vars
   WHERE project_id <> keep_project_id;

  -- ── Step 4: Delete supabase_connections except kept project ─────────────
  DELETE FROM supabase_connections
   WHERE project_id <> keep_project_id;

  -- ── Step 5: Delete dev_containers except kept project ───────────────────
  DELETE FROM dev_containers
   WHERE project_id <> keep_project_id;

  -- ── Step 6: Delete all other projects (FK cascade handles children) ─────
  -- Nullify fork lineage references pointing to the kept project so we can
  -- safely delete other projects without violating SET NULL FK behaviour.
  UPDATE projects
     SET forked_from_project_id = NULL
   WHERE forked_from_project_id = keep_project_id
     AND id <> keep_project_id;

  DELETE FROM projects
   WHERE id <> keep_project_id;

  -- ── Step 7: Delete all user_supabase_oauth ──────────────────────────────
  DELETE FROM user_supabase_oauth
   WHERE user_id <> keep_user_id;

  -- ── Step 8: Delete all user_provider_keys ───────────────────────────────
  DELETE FROM user_provider_keys
   WHERE user_id <> keep_user_id;

  -- ── Step 9: Delete all user_settings ────────────────────────────────────
  DELETE FROM user_settings
   WHERE user_id <> keep_user_id;

  -- ── Step 10: Delete all users except the showcase project owner ─────────
  -- Cascade will clean up any remaining child rows for the deleted users.
  DELETE FROM users
   WHERE id <> keep_user_id;

  -- ── Step 11: Reset the showcase project owner to a clean stub ───────────
  -- The kept user row now has no real password/email — you can leave it as-is
  -- for the showcase flow (it only serves as the FK anchor).

  RAISE NOTICE 'Done. Remaining projects: %, Remaining users: %',
    (SELECT count(*) FROM projects),
    (SELECT count(*) FROM users);

END $$;

COMMIT;

-- ── Verification queries (run after) ────────────────────────────────────────
-- SELECT id, name, showcased_at FROM projects;
-- SELECT id, email, onboarding_completed FROM users;
-- SELECT count(*) FROM dev_containers;
-- SELECT count(*) FROM snapshots;
