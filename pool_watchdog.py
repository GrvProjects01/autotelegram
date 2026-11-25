"""Detecta quando o pool HTTP do painel trava e decide como recuperar.

Incidente de 25/11/2026: as chamadas ao painel passaram a falhar com PoolTimeout
(nenhuma conexao livre) enquanto o heartbeat, que usa outro pool, seguia
"online". As publicacoes pararam ate o servico ser reiniciado.

Regra: PoolTimeouts em sequencia, sem NENHUMA resposta do painel entre eles.
  1) apos `recycle_after` segundos: recriar o cliente HTTP;
  2) se continuar ate `exit_after` segundos: encerrar o processo para o systemd
     (Restart=always) subir um novo.
Falhas comuns de rede ou o painel fora do ar nao geram PoolTimeout continuo e
sem resposta por si so, e qualquer resposta HTTP zera a contagem.
"""

import time


class PoolWatchdog:
    def __init__(
        self,
        recycle_after=90,
        exit_after=300,
        min_failures=5,
        quiet_reset=150,
        clock=time.monotonic,
    ):
        self.recycle_after = recycle_after
        self.exit_after = exit_after
        self.min_failures = min_failures
        # Falhas separadas por um intervalo maior que este nao sao a mesma trava.
        self.quiet_reset = quiet_reset
        self.clock = clock
        self.record_response()

    def record_response(self):
        self.failures = 0
        self.stalled_since = None
        self.last_failure = None
        self.last_recycle = None
        self.exit_requested = False

    def stalled_for(self):
        if self.stalled_since is None:
            return 0.0
        return self.clock() - self.stalled_since

    def record_pool_timeout(self):
        """Retorna None, "recycle" ou "exit"."""
        now = self.clock()

        if self.last_failure is not None and now - self.last_failure > self.quiet_reset:
            self.record_response()

        if self.stalled_since is None:
            self.stalled_since = now
        self.last_failure = now
        self.failures += 1

        if self.failures < self.min_failures or self.exit_requested:
            return None

        stalled = now - self.stalled_since

        if stalled >= self.exit_after:
            self.exit_requested = True
            return "exit"

        if stalled >= self.recycle_after and (
            self.last_recycle is None
            or now - self.last_recycle >= self.recycle_after
        ):
            self.last_recycle = now
            return "recycle"

        return None
