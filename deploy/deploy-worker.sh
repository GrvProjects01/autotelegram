#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/ubuntu/autotelegram/autotelegram}"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-production}"
DEPLOY_USER="${DEPLOY_USER:-ubuntu}"
VENV_DIR="${VENV_DIR:-$PROJECT_DIR/venv}"
SERVICE_TEMPLATE_SRC="$PROJECT_DIR/deploy/telegram-worker@.service"
SERVICE_TEMPLATE_DST="/etc/systemd/system/telegram-worker@.service"

log() {
  printf '[Deploy] %s\n' "$*"
}

fail() {
  printf '[Deploy] ERRO: %s\n' "$*" >&2
  exit 1
}

run_as_deploy_user() {
  if [[ "$(id -un)" == "$DEPLOY_USER" ]]; then
    "$@"
  else
    sudo -u "$DEPLOY_USER" "$@"
  fi
}

require_root_or_sudo() {
  if [[ "$(id -u)" -ne 0 ]] && ! sudo -n true 2>/dev/null; then
    fail "o deploy precisa rodar como root ou com sudo sem prompt"
  fi
}

require_root_or_sudo

[[ -d "$PROJECT_DIR/.git" ]] || fail "repositorio nao encontrado em $PROJECT_DIR"
[[ -f "$SERVICE_TEMPLATE_SRC" ]] || fail "template systemd ausente: $SERVICE_TEMPLATE_SRC"

log "repositorio: $PROJECT_DIR"
log "branch alvo: $DEPLOY_BRANCH"

DIRTY_TRACKED="$(run_as_deploy_user git -C "$PROJECT_DIR" status --porcelain --untracked-files=no)"
if [[ -n "$DIRTY_TRACKED" ]]; then
  printf '%s\n' "$DIRTY_TRACKED" >&2
  fail "existem alteracoes locais rastreadas; revise antes do deploy"
fi

log "buscando origin/$DEPLOY_BRANCH"
run_as_deploy_user git -C "$PROJECT_DIR" fetch --prune origin "$DEPLOY_BRANCH"

CURRENT_BRANCH="$(run_as_deploy_user git -C "$PROJECT_DIR" branch --show-current)"
if [[ "$CURRENT_BRANCH" != "$DEPLOY_BRANCH" ]]; then
  log "trocando branch $CURRENT_BRANCH -> $DEPLOY_BRANCH"
  run_as_deploy_user git -C "$PROJECT_DIR" checkout "$DEPLOY_BRANCH"
fi

log "aplicando fast-forward"
run_as_deploy_user git -C "$PROJECT_DIR" pull --ff-only origin "$DEPLOY_BRANCH"

DEPLOY_SHA="$(run_as_deploy_user git -C "$PROJECT_DIR" rev-parse HEAD)"
log "commit em deploy: $DEPLOY_SHA"

if [[ -f "$PROJECT_DIR/requirements.txt" ]]; then
  [[ -x "$VENV_DIR/bin/python" ]] || fail "venv nao encontrada em $VENV_DIR"
  log "sincronizando dependencias Python"
  run_as_deploy_user "$VENV_DIR/bin/python" -m pip install -r "$PROJECT_DIR/requirements.txt" --disable-pip-version-check
fi

log "validando sintaxe Python critica"
run_as_deploy_user "$VENV_DIR/bin/python" -m py_compile \
  "$PROJECT_DIR/session_worker_history.py" \
  "$PROJECT_DIR/session_worker.py" \
  "$PROJECT_DIR/worker.py" \
  "$PROJECT_DIR/recurring_messages_addon.py" \
  "$PROJECT_DIR/recurring_session_transport_addon.py"

if [[ -x "$VENV_DIR/bin/pytest" ]]; then
  TEST_FILES=()
  [[ -f "$PROJECT_DIR/tests/test_recurring_messages_addon.py" ]] && TEST_FILES+=("$PROJECT_DIR/tests/test_recurring_messages_addon.py")
  [[ -f "$PROJECT_DIR/tests/test_recurring_session_transport_addon.py" ]] && TEST_FILES+=("$PROJECT_DIR/tests/test_recurring_session_transport_addon.py")
  if [[ ${#TEST_FILES[@]} -gt 0 ]]; then
    log "testando scheduler recorrente"
    run_as_deploy_user "$VENV_DIR/bin/pytest" -q "${TEST_FILES[@]}"
  fi
fi

log "instalando template systemd"
if [[ "$(id -u)" -eq 0 ]]; then
  cp "$SERVICE_TEMPLATE_SRC" "$SERVICE_TEMPLATE_DST"
  systemctl daemon-reload
  systemctl disable --now telegram-worker 2>/dev/null || true
  systemctl reset-failed telegram-worker@primary telegram-worker@marca_b 2>/dev/null || true
  systemctl restart telegram-worker@primary
  systemctl restart telegram-worker@marca_b
else
  sudo cp "$SERVICE_TEMPLATE_SRC" "$SERVICE_TEMPLATE_DST"
  sudo systemctl daemon-reload
  sudo systemctl disable --now telegram-worker 2>/dev/null || true
  sudo systemctl reset-failed telegram-worker@primary telegram-worker@marca_b 2>/dev/null || true
  sudo systemctl restart telegram-worker@primary
  sudo systemctl restart telegram-worker@marca_b
fi

log "aguardando workers estabilizarem"
sleep 8

check_service() {
  local service="$1"
  local state
  if [[ "$(id -u)" -eq 0 ]]; then
    state="$(systemctl is-active "$service" || true)"
  else
    state="$(sudo systemctl is-active "$service" || true)"
  fi
  if [[ "$state" != "active" ]]; then
    if [[ "$(id -u)" -eq 0 ]]; then
      systemctl status "$service" --no-pager || true
      journalctl -u "$service" -n 80 --no-pager || true
    else
      sudo systemctl status "$service" --no-pager || true
      sudo journalctl -u "$service" -n 80 --no-pager || true
    fi
    fail "$service nao ficou active"
  fi
  log "$service: active"
}

check_service telegram-worker@primary
check_service telegram-worker@marca_b

log "processos ativos"
pgrep -af 'session_worker_history.py|session_worker.py|worker.py' || true

log "DEPLOY OK sha=$DEPLOY_SHA"
