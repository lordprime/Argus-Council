#!/usr/bin/env bash
# Tests for the deploy scripts (backup, update, rollback, restore) against a stub `docker`.
# Plain bash, no Docker needed:
#
#   ./scripts/tests/run.sh            # all tests
#   ./scripts/tests/run.sh rollback   # only tests whose name contains "rollback"
#
# Each test copies scripts/ into a fresh git checkout under a temp dir and puts stubs/ (docker,
# curl, sleep) first on PATH. The docker stub keeps the simulated images, containers and database
# in $STUB_STATE; see stubs/docker for the model.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
FILTER="${1:-}"
PASS=0 FAIL=0 FAILED=()
WORK="$(mktemp -d "${TMPDIR:-/tmp}/argus-deploy-tests.XXXXXX")"
if [ -n "${KEEP:-}" ]; then echo "keeping $WORK"; else trap 'rm -rf "$WORK"' EXIT; fi

# ---------------------------------------------------------------- fixture

# setup: a deployment running version v1 (migration head 1), database at revision 1.
setup() {
  T="$WORK/$1"
  R="$T/repo"
  export STUB_STATE="$T/state"
  mkdir -p "$R/deploy" "$R/backend" "$R/frontend" "$STUB_STATE"/{tags,ids,db,ct}
  cp -R "$REPO/scripts" "$R/"
  cp "$REPO/deploy/docker-compose.yml" "$R/deploy/"
  cp "$REPO/.gitignore" "$R/"
  cd "$T" || exit 1 # stray relative paths in a broken test land here, not in the checkout
  printf '%s\n' ARGUS_SECRET_KEY=x POSTGRES_PASSWORD=x REDIS_PASSWORD=x ARGUS_SANDBOX_TOKEN=x >"$R/deploy/.env"
  git -C "$R" init -q
  git -C "$R" add -A
  git -C "$R" -c user.name=t -c user.email=t@example.com commit -qm init
  printf 'head=1\nhealthy=1\n' >"$STUB_STATE/ids/sha256:v1-api"
  printf 'head=1\nhealthy=1\n' >"$STUB_STATE/ids/sha256:v1-web"
  echo sha256:v1-api >"$STUB_STATE/tags/argus-council_api_v1"
  echo sha256:v1-web >"$STUB_STATE/tags/argus-council_web_v1"
  echo sha256:v1-api >"$STUB_STATE/ct/api"
  echo sha256:v1-web >"$STUB_STATE/ct/web"
  echo healthy >"$STUB_STATE/ct/api.status"
  touch "$STUB_STATE/postgres_up"
  echo 1 >"$STUB_STATE/db/rev"
  echo d1 >"$STUB_STATE/db/data"
  echo a1 >"$STUB_STATE/artifacts"
  echo v1 >"$R/deploy/.version"
  unset STUB_BUILD_HEAD STUB_BUILD_HEALTHY STUB_BUILD_FAIL STUB_PGDUMP_FAIL STUB_TAR_FAIL \
    STUB_FORCE_UNHEALTHY STUB_DOWNGRADE_FAIL ARGUS_BACKUP_DIR ARGUS_BACKUP_KEEP ARGUS_READY_TIMEOUT
  export ARGUS_READY_TIMEOUT=20 # a broken version is given up on after 20 polls, not 240
}

# run SCRIPT ARGS...: run a copied script with the stubs first on PATH; output goes to $T/out.
run() {
  local script="$1"; shift
  PATH="$HERE/stubs:$PATH" bash "$R/scripts/$script" "$@" >"$T/out" 2>&1 </dev/null
}

st() { cat "$STUB_STATE/$1" 2>/dev/null; }          # stub state value
running_api() { st ct/api; }
tag_id() { st "tags/$(printf '%s' "$1" | tr '/:' '__')"; }
count() { [ -f "$STUB_STATE/$1" ] && wc -l <"$STUB_STATE/$1" | tr -d ' ' || echo 0; }
dumps() { find "$R/backups" -name 'argus-db-*.dump' 2>/dev/null | sort; }

# ---------------------------------------------------------------- assertions

CUR="" CUR_OK=1
check() { # check DESCRIPTION COMMAND...
  local what="$1"; shift
  if ! "$@"; then
    CUR_OK=0
    printf '    FAIL: %s\n' "$what"
  fi
}
eq() { [ "$1" = "$2" ] || { printf '    expected [%s], got [%s]\n' "$2" "$1"; return 1; }; }
contains() { grep -qF -- "$2" "$1" || { printf '    [%s] not in %s\n' "$2" "${1##*/}"; return 1; }; }
lacks() { ! grep -qF -- "$2" "$1" || { printf '    [%s] unexpectedly in %s\n' "$2" "${1##*/}"; return 1; }; }
ok() { "$@"; }
fails() { ! "$@"; }

begin() {
  CUR="$1" CUR_OK=1
  case "$CUR" in *"$FILTER"*) ;; *) return 1 ;; esac
  printf '  %s\n' "$CUR"
  setup "$CUR"
}
end() {
  if [ "$CUR_OK" = 1 ]; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1)) FAILED+=("$CUR")
    printf '    --- script output ---\n'
    sed 's/^/    | /' "$T/out" 2>/dev/null | tail -n 40
  fi
}

# ---------------------------------------------------------------- tests

echo "deploy script tests"

if begin backup_writes_verified_files_with_checksums; then
  check "backup.sh succeeds" ok run backup.sh
  dump="$(dumps | tail -n1)"
  check "a dump exists" test -s "$dump"
  check "the dump has a checksum file" test -f "$dump.sha256"
  check "the checksum matches" eq "$(cut -d' ' -f1 <"$dump.sha256")" "$(sha256sum "$dump" | cut -d' ' -f1)"
  check "sha256sum -c accepts it" ok bash -c "cd '$R/backups' && sha256sum -c --quiet '${dump##*/}.sha256'"
  tgz="${dump/argus-db-/argus-data-}"; tgz="${tgz%.dump}.tgz"
  check "the artifact tarball exists" test -s "$tgz"
  check "the tarball has a checksum file" test -f "$tgz.sha256"
  check "no .partial left" eq "$(find "$R/backups" -name '*.partial' | wc -l | tr -d ' ')" 0
  check "the dump path is printed last" eq "$(tail -n1 "$T/out")" "$dump"
  check "the lock is released" fails test -e "$R/deploy/.lock"
  end
fi

if begin backup_failed_pg_dump_leaves_no_dump; then
  run backup.sh
  first="$(dumps)"
  export STUB_PGDUMP_FAIL=1
  check "backup.sh fails" fails run backup.sh
  check "only the earlier dump remains" eq "$(dumps)" "$first"
  check "no .partial left" eq "$(find "$R/backups" -name '*.partial' | wc -l | tr -d ' ')" 0
  check "the error is reported" contains "$T/out" "pg_dump failed"
  end
fi

if begin backup_two_in_one_second_do_not_collide; then
  run backup.sh; run backup.sh
  check "two dumps" eq "$(dumps | wc -l | tr -d ' ')" 2
  end
fi

if begin backup_prunes_old_and_partial_files; then
  export ARGUS_BACKUP_KEEP=2
  mkdir -p "$R/backups"
  echo junk >"$R/backups/argus-db-20200101T000000Z.dump.partial"
  run backup.sh; run backup.sh; run backup.sh
  check "only 2 dumps kept" eq "$(dumps | wc -l | tr -d ' ')" 2
  check "pruned checksums removed too" eq "$(find "$R/backups" -name 'argus-db-*.sha256' | wc -l | tr -d ' ')" 2
  check "stale .partial removed" fails test -e "$R/backups/argus-db-20200101T000000Z.dump.partial"
  end
fi

if begin update_success_with_migration; then
  export STUB_BUILD_HEAD=2
  check "update.sh succeeds" ok run update.sh --no-pull
  next="$(cat "$R/deploy/.built-version")"
  check "the new version is recorded" eq "$(cat "$R/deploy/.version")" "$next"
  check "the new image runs" eq "$(running_api)" "$(tag_id "argus-council/api:$next")"
  check "the database migrated" eq "$(st db/rev)" 2
  prev="$(cat "$R/deploy/.previous-version")"
  case "$prev" in rollback-*) r=0 ;; *) r=1 ;; esac
  check "the previous version is a snapshot tag ($prev)" eq "$r" 0
  check "the snapshot points at the old image" eq "$(tag_id "argus-council/api:$prev")" sha256:v1-api
  check "the rollback state records the old revision" contains "$R/deploy/.rollback-state" "db_revision=1"
  check "the rollback state records the dump" contains "$R/deploy/.rollback-state" "db_dump=$(dumps | tail -n1)"
  end
fi

if begin update_failed_after_migration_restores_db_and_old_image; then
  echo d-before >"$STUB_STATE/db/data"
  export STUB_BUILD_HEAD=3 STUB_BUILD_HEALTHY=0
  check "update.sh fails" fails run update.sh --no-pull
  check "it says it rolled back" contains "$T/out" "update failed and was rolled back"
  check "the pre-update dump was restored" eq "$(count restores.log)" 1
  check "the database is back at revision 1" eq "$(st db/rev)" 1
  check "the data is the pre-update data" eq "$(st db/data)" d-before
  check "the old image runs again" eq "$(running_api)" sha256:v1-api
  check "and it is healthy" eq "$(st ct/api.status)" healthy
  check "the version file names the old images" eq "$(tag_id "argus-council/api:$(cat "$R/deploy/.version")")" sha256:v1-api
  check "no rollback state from the failed update" fails test -f "$R/deploy/.rollback-state"
  check "the API was stopped before the restore" contains "$STUB_STATE/calls.log" "stop api web"
  end
fi

if begin update_failed_after_an_earlier_update_keeps_its_rollback; then
  export STUB_BUILD_HEAD=2
  run update.sh --no-pull
  good="$(cat "$R/deploy/.built-version")"
  good_id="$(tag_id "argus-council/api:$good")"
  # Same commit again, so the broken build overwrites the tag "$good" (M13).
  export STUB_BUILD_HEAD=3 STUB_BUILD_HEALTHY=0
  check "the second update fails" fails run update.sh --no-pull
  check "the build reused the tag" eq "$(cat "$R/deploy/.built-version")" "$good"
  check "the good image runs at revision 2" eq "$(running_api):$(st ct/api.status):$(st db/rev)" "$good_id:healthy:2"
  check "the first update's record is back" contains "$R/deploy/.rollback-state" "db_revision=1"
  from="$(sed -n 's/^from=//p' "$R/deploy/.rollback-state")"
  check "its 'from' names the running images, not the overwritten tag" eq "$(tag_id "argus-council/api:$from")" "$good_id"
  unset STUB_BUILD_HEAD STUB_BUILD_HEALTHY
  check "--rollback still reaches v1" ok run update.sh --rollback
  check "v1 runs at revision 1" eq "$(running_api):$(st ct/api.status):$(st db/rev)" sha256:v1-api:healthy:1
  end
fi

if begin update_failed_without_migration_does_not_restore; then
  export STUB_BUILD_HEAD=1 STUB_BUILD_HEALTHY=0
  check "update.sh fails" fails run update.sh --no-pull
  check "no restore (schema unchanged)" eq "$(count restores.log)" 0
  check "the old image runs again" eq "$(running_api)" sha256:v1-api
  check "and it is healthy" eq "$(st ct/api.status)" healthy
  end
fi

if begin update_failed_no_backup_downgrades_with_new_image; then
  export STUB_BUILD_HEAD=2 STUB_BUILD_HEALTHY=0
  check "update.sh fails" fails run update.sh --no-pull --no-backup
  check "no backup taken" eq "$(dumps | wc -l | tr -d ' ')" 0
  check "alembic downgrade ran" eq "$(count downgrades.log)" 1
  check "with the new image" contains "$STUB_STATE/calls.log" "run --rm --no-deps -T -e ARGUS_RUN_MIGRATIONS=false api alembic downgrade 1"
  check "the database is back at revision 1" eq "$(st db/rev)" 1
  check "the old image runs and is healthy" eq "$(running_api):$(st ct/api.status)" sha256:v1-api:healthy
  end
fi

if begin update_failed_bad_dump_falls_back_to_downgrade; then
  export STUB_BUILD_HEAD=2 STUB_BUILD_HEALTHY=0
  # Corrupt the dump between the backup and the rollback: the build is the window.
  mkdir -p "$STUB_STATE"
  cat >"$T/corrupt-build" <<'EOF'
#!/usr/bin/env bash
for f in "$ROOT_UNDER_TEST"/backups/argus-db-*.dump; do echo garbage >>"$f"; done
exec "$REAL_STUB" "$@"
EOF
  mkdir -p "$T/bin"; cp "$T/corrupt-build" "$T/bin/docker"; chmod +x "$T/bin/docker"
  ROOT_UNDER_TEST="$R" REAL_STUB="$HERE/stubs/docker" PATH="$T/bin:$HERE/stubs:$PATH" \
    bash "$R/scripts/update.sh" --no-pull >"$T/out" 2>&1 </dev/null
  check "update.sh reports the rollback" contains "$T/out" "update failed and was rolled back"
  check "the corrupt dump was refused" contains "$T/out" "checksum mismatch"
  check "no restore from it" eq "$(count restores.log)" 0
  check "downgrade used instead" eq "$(count downgrades.log)" 1
  check "the database is back at revision 1" eq "$(st db/rev)" 1
  end
fi

if begin update_build_reusing_tag_rolls_back_to_old_image; then
  # Clean checkout, same commit: the build reuses the running tag and overwrites it (M13).
  sha="$(git -C "$R" rev-parse --short HEAD)"
  echo sha256:v1-api >"$STUB_STATE/tags/argus-council_api_$sha"
  echo sha256:v1-web >"$STUB_STATE/tags/argus-council_web_$sha"
  echo "$sha" >"$R/deploy/.version"
  export STUB_BUILD_HEAD=1 STUB_BUILD_HEALTHY=0
  check "update.sh fails" fails run update.sh --no-pull
  check "the build reused the tag" eq "$(cat "$R/deploy/.built-version")" "$sha"
  check "the tag was overwritten by the build" ok bash -c "grep -q built '$STUB_STATE/tags/argus-council_api_$sha'"
  check "but the old image runs again" eq "$(running_api)" sha256:v1-api
  check "and it is healthy" eq "$(st ct/api.status)" healthy
  end
fi

if begin update_with_prebuilt_images_and_their_rollback; then
  # What CI does against real containers: deploy existing images (no build) that migrate and fail.
  printf 'head=2\nhealthy=0\n' >"$STUB_STATE/ids/sha256:broken-api"
  echo sha256:broken-api >"$STUB_STATE/tags/argus-council_api_broken"
  echo sha256:v1-web >"$STUB_STATE/tags/argus-council_web_broken"
  export ARGUS_READY_TIMEOUT=5
  check "update.sh fails" fails run update.sh --no-pull --images broken
  check "nothing was built" fails test -f "$STUB_STATE/build_counter"
  check "the images were tagged as the version" eq "$(tag_id "argus-council/api:$(cat "$R/deploy/.built-version")")" sha256:broken-api
  check "the new version got 5s" contains "$T/out" "up to 5s"
  check "the dump was restored" eq "$(count restores.log)" 1
  check "the old image runs at revision 1" eq "$(running_api):$(st ct/api.status):$(st db/rev)" sha256:v1-api:healthy:1
  check "--images with a missing tag fails" fails run update.sh --no-pull --images missing
  check "with a clear error" contains "$T/out" "no image argus-council/api:missing"
  unset ARGUS_READY_TIMEOUT
  end
fi

if begin git_version_marks_untracked_files_dirty; then
  sha="$(git -C "$R" rev-parse --short HEAD)"
  v1="$(bash -c "source '$R/scripts/lib.sh'; git_version")"
  check "clean checkout: plain sha" eq "$v1" "$sha"
  echo new >"$R/scripts/new_migration.py"
  v2="$(bash -c "source '$R/scripts/lib.sh'; git_version")"
  case "$v2" in "$sha"-dirty-*) r=0 ;; *) r=1 ;; esac
  check "untracked file: dirty with a timestamp ($v2)" eq "$r" 0
  rm "$R/scripts/new_migration.py"
  echo "# edit" >>"$R/scripts/smoke.sh"
  v3="$(bash -c "source '$R/scripts/lib.sh'; git_version")"
  case "$v3" in "$sha"-dirty-*) r=0 ;; *) r=1 ;; esac
  check "modified file: dirty ($v3)" eq "$r" 0
  end
fi

if begin rollback_after_successful_update_downgrades_then_rolls_forward; then
  export STUB_BUILD_HEAD=2
  run update.sh --no-pull
  next="$(cat "$R/deploy/.built-version")"
  new_id="$(tag_id "argus-council/api:$next")"
  unset STUB_BUILD_HEAD
  check "--rollback succeeds" ok run update.sh --rollback
  check "schema downgraded to 1" eq "$(st db/rev)" 1
  check "by alembic downgrade (keeps data)" eq "$(count downgrades.log)" 1
  check "no restore" eq "$(count restores.log)" 0
  check "the old image runs" eq "$(running_api):$(st ct/api.status)" sha256:v1-api:healthy
  check "--rollback again rolls forward" ok run update.sh --rollback
  check "the new image runs" eq "$(running_api):$(st ct/api.status)" "$new_id:healthy"
  check "and migrated again" eq "$(st db/rev)" 2
  check "without another downgrade" eq "$(count downgrades.log)" 1
  end
fi

if begin rollback_restore_db_loads_pre_update_dump; then
  echo d-before >"$STUB_STATE/db/data"
  export STUB_BUILD_HEAD=2
  run update.sh --no-pull
  echo d-after-update >"$STUB_STATE/db/data"
  check "--rollback --restore-db succeeds" ok run update.sh --rollback --restore-db
  check "restored once" eq "$(count restores.log)" 1
  check "pre-update data" eq "$(st db/data)" d-before
  check "revision 1" eq "$(st db/rev)" 1
  check "the old image runs" eq "$(running_api):$(st ct/api.status)" sha256:v1-api:healthy
  end
fi

if begin rollback_is_idempotent_after_a_failed_rollback; then
  export STUB_BUILD_HEAD=2 STUB_BUILD_HEALTHY=0 STUB_FORCE_UNHEALTHY=1
  check "update.sh fails" fails run update.sh --no-pull
  check "it says the rollback is not healthy" contains "$T/out" "is not healthy either"
  check "the schema was reverted anyway" eq "$(st db/rev)" 1
  check "the rollback state is kept" contains "$R/deploy/.rollback-state" "db_revision=1"
  unset STUB_FORCE_UNHEALTHY
  check "--rollback --restore-db retries" ok run update.sh --rollback --restore-db
  check "the database is not restored a second time" eq "$(count restores.log)" 1
  check "it notices the schema is already right" contains "$T/out" "already at revision 1"
  check "the old image runs" eq "$(running_api):$(st ct/api.status)" sha256:v1-api:healthy
  end
fi

if begin update_refuses_when_backup_fails; then
  export STUB_PGDUMP_FAIL=1 STUB_BUILD_HEAD=2
  check "update.sh fails" fails run update.sh --no-pull
  check "it says why" contains "$T/out" "the backup failed: not updating"
  check "nothing was built" fails test -f "$STUB_STATE/build_counter"
  check "the old image still runs" eq "$(running_api):$(st db/rev)" sha256:v1-api:1
  end
fi

if begin update_refuses_while_locked; then
  mkdir -p "$R/deploy/.lock"; echo $$ >"$R/deploy/.lock/pid"
  check "update.sh fails" fails run update.sh --no-pull
  check "because of the lock" contains "$T/out" "another update, backup or restore is running"
  echo 999999 >"$R/deploy/.lock/pid"
  check "a stale lock is taken over" ok run backup.sh
  end
fi

if begin restore_restores_db_and_artifacts; then
  run backup.sh
  echo d-later >"$STUB_STATE/db/data"; echo 2 >"$STUB_STATE/db/rev"; echo a-later >"$STUB_STATE/artifacts"
  printf 'head=2\nhealthy=1\n' >"$STUB_STATE/ids/sha256:v1-api"
  check "restore.sh --yes succeeds" ok run restore.sh --yes
  check "the dump was loaded at revision 1" contains "$T/out" "database at revision 1"
  check "then the running image (head 2) migrated it" eq "$(st db/data):$(st db/rev)" d1+m2:2
  check "artifacts restored" eq "$(st artifacts)" a1
  check "the schema is dropped and the dump loaded in one transaction" \
    ok bash -c "head -n2 '$STUB_STATE/last.sql' | tr '\n' ' ' | grep -q 'BEGIN; DROP SCHEMA IF EXISTS public CASCADE;' && tail -n1 '$STUB_STATE/last.sql' | grep -qx 'COMMIT;'"
  check "the API runs again" eq "$(st ct/api.status)" healthy
  end
fi

if begin restore_refuses_checksum_mismatch; then
  run backup.sh
  dump="$(dumps | tail -n1)"
  printf 'X' | dd of="$dump" bs=1 seek=12 conv=notrunc 2>/dev/null
  echo d-later >"$STUB_STATE/db/data"
  check "restore.sh fails" fails run restore.sh --yes
  check "because of the checksum" contains "$T/out" "checksum mismatch"
  check "the database is untouched" eq "$(st db/data)" d-later
  check "the API was not stopped" lacks "$STUB_STATE/calls.log" "stop api web"
  end
fi

if begin restore_refuses_truncated_dump_even_with_matching_checksum; then
  run backup.sh
  dump="$(dumps | tail -n1)"
  head -n2 "$dump" >"$dump.t" && mv "$dump.t" "$dump"
  printf '%s  %s\n' "$(sha256sum "$dump" | cut -d' ' -f1)" "${dump##*/}" >"$dump.sha256"
  echo d-later >"$STUB_STATE/db/data"
  check "restore.sh fails" fails run restore.sh --yes --db-only
  check "because the archive is truncated" contains "$T/out" "not a complete pg_dump archive"
  check "the database is untouched" eq "$(st db/data)" d-later
  end
fi

if begin restore_refuses_truncated_tarball; then
  run backup.sh
  dump="$(dumps | tail -n1)"
  tgz="${dump/argus-db-/argus-data-}"; tgz="${tgz%.dump}.tgz"
  head -c 20 "$tgz" >"$tgz.t" && mv "$tgz.t" "$tgz"
  printf '%s  %s\n' "$(sha256sum "$tgz" | cut -d' ' -f1)" "${tgz##*/}" >"$tgz.sha256"
  check "restore.sh fails" fails run restore.sh --yes
  check "because the tarball is truncated" contains "$T/out" "not a complete gzip tarball"
  check "--db-only still works" ok run restore.sh --yes --db-only
  end
fi

if begin restore_accepts_legacy_dump_without_checksum; then
  run backup.sh
  dump="$(dumps | tail -n1)"
  rm -f "$dump.sha256"
  echo d-later >"$STUB_STATE/db/data"
  check "restore.sh succeeds" ok run restore.sh --yes --db-only
  check "with a warning" contains "$T/out" "no checksum for"
  check "data restored" eq "$(st db/data)" d1
  end
fi

if begin restore_list_shows_checksum_state; then
  run backup.sh
  check "restore.sh --list succeeds" ok run restore.sh --list
  check "checksums ok" contains "$T/out" "db:ok data:ok"
  end
fi

if begin restore_restarts_api_when_the_restore_fails; then
  run backup.sh
  dump="$(dumps | tail -n1)"
  # A dump that passes verification but whose load fails: make psql see ROLLBACK by failing
  # the second pg_restore (the SQL conversion) only.
  mkdir -p "$T/bin"
  cat >"$T/bin/docker" <<'EOF'
#!/usr/bin/env bash
case "$*" in *"pg_restore --no-owner -f -"*) cat >/dev/null; echo "pg_restore: error" >&2; exit 1 ;; esac
exec "$REAL_STUB" "$@"
EOF
  chmod +x "$T/bin/docker"
  echo d-later >"$STUB_STATE/db/data"
  REAL_STUB="$HERE/stubs/docker" PATH="$T/bin:$HERE/stubs:$PATH" \
    bash "$R/scripts/restore.sh" --yes --db-only >"$T/out" 2>&1 </dev/null
  check "restore.sh failed" eq "$?" 1
  check "the transaction was rolled back" ok grep -qx 'ROLLBACK;' "$STUB_STATE/last.sql"
  check "the database is untouched" eq "$(st db/data)" d-later
  check "the API was started again" eq "$(st ct/api.status)" healthy
  end
fi

echo
printf '%d passed, %d failed\n' "$PASS" "$FAIL"
if [ "$FAIL" -gt 0 ]; then
  printf '  failed: %s\n' "${FAILED[@]}"
  exit 1
fi
