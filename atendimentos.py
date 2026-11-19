import csv
import json
import re
import shutil
import sqlite3
import sys
import threading
import traceback
from contextlib import closing
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else BASE
DB_PATH = APP_DIR / "atendimentos.db"
HTML_PATH = BASE / "interface.html"
EXPORT_DIR = APP_DIR / "exportacoes"
DB_INIT_MARKER = APP_DIR / ".atendimentos-db-inicializado"
FMT = "%Y-%m-%d %H:%M"


# ---------- banco de dados ----------
def preparar_banco():
    if not getattr(sys, "frozen", False) or DB_INIT_MARKER.exists():
        return

    banco_inicial = BASE / "atendimentos.db"
    if banco_inicial != DB_PATH and banco_inicial.exists():
        with closing(sqlite3.connect(DB_PATH)) as con:
            tabela = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='atendimentos'"
            ).fetchone()
            atual = con.execute("SELECT COUNT(*) FROM atendimentos").fetchone()[0] if tabela else 0
        with closing(sqlite3.connect(banco_inicial)) as con:
            inicial = con.execute("SELECT COUNT(*) FROM atendimentos").fetchone()[0]

        if atual == 0 and inicial:
            shutil.copy2(banco_inicial, DB_PATH)

    DB_INIT_MARKER.write_text("ok", encoding="utf-8")


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute(
        """CREATE TABLE IF NOT EXISTS atendimentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT NOT NULL,
            nome TEXT NOT NULL,
            loja TEXT NOT NULL,
            problema TEXT NOT NULL,
            resolvido INTEGER NOT NULL DEFAULT 0
        )"""
    )
    return con


def listar():
    with closing(db()) as con:
        linhas = con.execute(
            "SELECT id, data, nome, loja, problema, resolvido "
            "FROM atendimentos ORDER BY data DESC, id DESC"
        ).fetchall()
    return [
        {
            "id": r["id"],
            "data": r["data"].replace(" ", "T"),
            "nome": r["nome"],
            "loja": r["loja"],
            "problema": r["problema"],
            "resolvido": bool(r["resolvido"]),
        }
        for r in linhas
    ]


def limpar_campos(d):
    nome = str(d.get("nome", "")).strip()
    loja = str(d.get("loja", "")).strip()
    problema = str(d.get("problema", "")).strip()
    if not (nome and loja and problema):
        raise ValueError("Preencha nome, loja e problema.")
    return nome[:200], loja[:200], problema[:5000], 1 if d.get("resolvido") else 0


def criar(d):
    nome, loja, problema, resolvido = limpar_campos(d)
    with closing(db()) as con:
        con.execute(
            "INSERT INTO atendimentos (data, nome, loja, problema, resolvido) VALUES (?,?,?,?,?)",
            (datetime.now().strftime(FMT), nome, loja, problema, resolvido),
        )
        con.commit()


def alternar(id_):
    with closing(db()) as con:
        con.execute("UPDATE atendimentos SET resolvido = 1 - resolvido WHERE id = ?", (id_,))
        con.commit()


def excluir(id_):
    with closing(db()) as con:
        con.execute("DELETE FROM atendimentos WHERE id = ?", (id_,))
        con.commit()


def normalizar_data(valor):
    """Aceita '2026-11-18 14:30', ISO local ou ISO em UTC (backup da versão antiga)."""
    try:
        dt = datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt.strftime(FMT)
    except ValueError:
        return datetime.now().strftime(FMT)


def restaurar(lista):
    if not isinstance(lista, list):
        raise ValueError("Arquivo de backup inválido.")
    registros = []
    for d in lista:
        if not isinstance(d, dict):
            raise ValueError("Arquivo de backup inválido.")
        nome, loja, problema, resolvido = limpar_campos(d)
        registros.append((normalizar_data(d.get("data")), nome, loja, problema, resolvido))
    with closing(db()) as con:
        with con:  # transação: ou restaura tudo ou nada
            con.execute("DELETE FROM atendimentos")
            con.executemany(
                "INSERT INTO atendimentos (data, nome, loja, problema, resolvido) VALUES (?,?,?,?,?)",
                registros,
            )
    return len(registros)


def exportar(tipo):
    EXPORT_DIR.mkdir(exist_ok=True)
    carimbo = datetime.now().strftime("%Y%m%d-%H%M%S")
    dados = listar()
    if tipo == "csv":
        caminho = EXPORT_DIR / f"atendimentos-{carimbo}.csv"
        with open(caminho, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["Data", "Nome", "Loja", "Problema", "Situação"])
            for d in dados:
                data = datetime.strptime(d["data"], "%Y-%m-%dT%H:%M").strftime("%d/%m/%Y %H:%M")
                w.writerow([data, d["nome"], d["loja"], d["problema"],
                            "Resolvido" if d["resolvido"] else "Pendente"])
    elif tipo == "json":
        caminho = EXPORT_DIR / f"backup-{carimbo}.json"
        caminho.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        raise ValueError("Tipo de exportação inválido.")
    return str(caminho)


# ---------- servidor local (só aceita conexões da própria máquina) ----------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _autorizado(self):
        # bloqueia requisições vindas de outros sites abertos no navegador
        origem = self.headers.get("Origin")
        return self.headers.get("Host") == self.server.host and (
            origem is None or origem == "http://" + self.server.host
        )

    def _enviar(self, corpo, codigo=200, tipo="application/json; charset=utf-8"):
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corpo)

    def _json(self, obj, codigo=200):
        self._enviar(json.dumps(obj, ensure_ascii=False).encode("utf-8"), codigo)

    def _corpo(self):
        tamanho = int(self.headers.get("Content-Length") or 0)
        if tamanho > 10_000_000:
            raise ValueError("Arquivo muito grande.")
        bruto = self.rfile.read(tamanho)
        return json.loads(bruto) if bruto else None

    def _tratar(self, funcao):
        if not self._autorizado():
            return self._json({"erro": "Acesso negado."}, 403)
        try:
            self._json(funcao())
        except (ValueError, json.JSONDecodeError) as e:
            self._json({"erro": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            self._json({"erro": f"Erro interno: {e}"}, 500)

    def do_GET(self):
        if not self._autorizado():
            return self._json({"erro": "Acesso negado."}, 403)
        if self.path in ("/", "/index.html"):
            try:
                self._enviar(HTML_PATH.read_bytes(), tipo="text/html; charset=utf-8")
            except FileNotFoundError:
                self._enviar("interface.html não encontrado ao lado do atendimentos.py".encode(), 500, "text/plain; charset=utf-8")
        elif self.path == "/api/atendimentos":
            self._tratar(listar)
        else:
            self._json({"erro": "Não encontrado."}, 404)

    def do_POST(self):
        caminho = self.path

        def acao():
            if caminho == "/api/atendimentos":
                criar(self._corpo() or {})
                return {"ok": True}
            if caminho == "/api/restaurar":
                return {"ok": True, "total": restaurar(self._corpo())}
            m = re.fullmatch(r"/api/exportar/(\w+)", caminho)
            if m:
                return {"ok": True, "caminho": exportar(m.group(1))}
            m = re.fullmatch(r"/api/atendimentos/(\d+)/alternar", caminho)
            if m:
                alternar(int(m.group(1)))
                return {"ok": True}
            raise ValueError("Rota inválida.")

        self._tratar(acao)

    def do_DELETE(self):
        m = re.fullmatch(r"/api/atendimentos/(\d+)", self.path)

        def acao():
            if not m:
                raise ValueError("Rota inválida.")
            excluir(int(m.group(1)))
            return {"ok": True}

        self._tratar(acao)


def main():
    servidor = None
    try:
        preparar_banco()
        servidor = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        servidor.host = f"127.0.0.1:{servidor.server_address[1]}"
        url = f"http://{servidor.host}/"
        threading.Thread(target=servidor.serve_forever, daemon=True).start()

        import webview
        webview.create_window(
            "Registro de Atendimentos",
            url,
            width=1200,
            height=800,
            min_size=(900, 600),
        )
        webview.start(gui="edgechromium")
    except BaseException:
        (APP_DIR / "startup-error.log").write_text(
            traceback.format_exc(),
            encoding="utf-8",
        )
        raise
    finally:
        if servidor is not None:
            servidor.shutdown()
            servidor.server_close()


if __name__ == "__main__":
    main()
