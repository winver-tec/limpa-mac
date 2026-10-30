#!/usr/bin/env python3
"""Limpa Mac — painel local para ver o que ocupa memória e disco e mandar para a Lixeira com um clique.

Uso:  python3 app.py        (abre http://127.0.0.1:8765 no navegador)
Nada é apagado direto: "Mover para a Lixeira" faz mv para ~/.Trash; esvaziar a Lixeira é um botão separado,
com confirmação. Cada ação fica registrada em historico.jsonl ao lado deste arquivo.
"""
import json, os, shutil, subprocess, threading, time, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
AQUI = Path(__file__).resolve().parent
HIST = AQUI / "historico.jsonl"
PORTA = 8765
TRASH = HOME / ".Trash"
PROJETOS = HOME / "Documents" / "Projetos"

# Nunca entra na lista, nem por engano.
INTOCAVEIS = [HOME / ".config" / "winver", HOME / ".config" / "marvee", HOME / ".ssh", HOME / "Library" / "Keychains",
              HOME / ".claude" / "projects", HOME / "Documents" / "Projetos" / "Winver" / ".maestri"]

estado = {"varrendo": False, "atualizado": None, "itens": [], "erro": None}
conhecidos = {}  # caminho -> item, de todas as varreduras desta execução
trava = threading.Lock()


def sh(cmd, cwd=None, timeout=120):
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def tamanho(p):
    out = sh(["du", "-sk", str(p)], timeout=600)
    try:
        return int(out.split()[0]) * 1024
    except Exception:
        return 0


def git_seguro(p):
    """Worktree/repo só é 'seguro' se não tem alteração nem commit fora do remoto."""
    if not (p / ".git").exists():
        return None, "sem git"
    sujo = sh(["git", "status", "--porcelain"], cwd=p)
    fora = sh(["git", "rev-list", "--count", "HEAD", "--not", "--remotes"], cwd=p)
    ramo = sh(["git", "branch", "--show-current"], cwd=p) or "(sem branch)"
    n = int(fora) if fora.isdigit() else -1
    if sujo:
        return False, f"{ramo}: tem alterações não salvas no git"
    if n != 0:
        return False, f"{ramo}: {n if n > 0 else '?'} commit(s) que não estão no GitHub"
    return True, f"{ramo}: limpo e tudo no GitHub"


def item(caminho, grupo, por_que, nivel="seguro", obs=""):
    p = Path(caminho)
    if not p.exists() or any(str(p).startswith(str(i)) for i in INTOCAVEIS):
        return None
    return {"caminho": str(p), "nome": str(p).replace(str(HOME), "~"), "grupo": grupo, "por_que": por_que,
            "nivel": nivel, "obs": obs, "bytes": tamanho(p)}


def varrer():
    itens = []
    # 1) Worktrees do superpowers (§40 proíbe cópias paralelas)
    wt = HOME / ".config" / "superpowers" / "worktrees"
    for repo in sorted(wt.glob("*/*")) if wt.exists() else []:
        ok, obs = git_seguro(repo)
        itens.append(item(repo, "Cópias de repositório (worktrees)", "Cópia paralela antiga de um repo (vetada pela §40).",
                          "seguro" if ok else "confirmar", obs))
    # 2) Downloads grandes
    dl = HOME / "Downloads"
    for f in dl.iterdir() if dl.exists() else []:
        if f.name.startswith("."):
            continue
        it = item(f, "Downloads", "Arquivo baixado.", "confirmar")
        if it and it["bytes"] > 100 * 1024 * 1024:
            itens.append(it)
    # 3) Ferramentas de desenvolvimento (recriáveis)
    for c, g, w in [
        (HOME / "Library/Developer/Xcode/DerivedData", "Desenvolvimento", "Build do Xcode; recria sozinho."),
        
        (HOME / ".npm/_cacache", "Caches", "Cache do npm; baixa de novo quando precisar."),
        
        (HOME / "Library/Caches/Google", "Caches", "Cache do Chrome."),
        (HOME / "Library/Caches/CocoaPods", "Caches", "Cache do CocoaPods."),
        (HOME / "Library/Caches/ms-playwright", "Caches", "Navegadores de teste do Playwright."),
        (HOME / "Library/Caches/electron", "Caches", "Cache do Electron."),
        (HOME / "Library/Application Support/com.docker.install", "Docker", "Instaladores antigos do Docker."),
        (HOME / ".expo", "Caches", "Cache do Expo (app mobile)."),
    ]:
        it = item(c, g, w, "seguro")
        if it:
            itens.append(it)
    for c, w in [(HOME / "Library/Developer/CoreSimulator/Devices", "Simuladores de iPhone. O time mobile (Tiago, Samuel) precisa recriar no Xcode."),
                 (HOME / ".cache/puppeteer", "Navegador de teste do Puppeteer; baixa de novo."),
                 (HOME / ".cache/codex-runtimes", "Runtime do Codex; o app do Codex baixa de novo se precisar.")]:
        it = item(c, "A confirmar", w, "confirmar")
        if it:
            itens.append(it)
    docker = HOME / "Library/Containers/com.docker.docker"
    it = item(docker, "Docker", "Disco virtual do Docker (imagens, bancos locais). Apagar zera os bancos locais.", "confirmar",
              "Prefira limpar pelo Docker: Troubleshoot > Clean / Purge data")
    if it:
        itens.append(it)
    # 4) Builds e node_modules dos projetos (recriáveis com npm install / build)
    if PROJETOS.exists():
        for nome, w in [("node_modules", "Dependências; voltam com npm install."), ("dist", "Build; recria no próximo build."),
                        (".next", "Build do Next; recria."), (".open-next", "Build do Cloudflare; recria."), ("out", "Build; recria.")]:
            for p in sh(["find", str(PROJETOS), "-maxdepth", "6", "-type", "d", "-name", nome, "-not", "-path", "*/node_modules/*"],
                        timeout=300).splitlines():
                it = item(p, "Projetos: " + nome, w, "seguro" if nome != "node_modules" else "confirmar")
                if it and it["bytes"] > 50 * 1024 * 1024:
                    itens.append(it)
    itens.extend(varrer_apps())
    itens = [i for i in itens if i]
    itens.sort(key=lambda i: -i["bytes"])
    return itens


DIAS_SEM_USO = 90


def dados_do_app(app, bundle):
    """Pastas de dados que o app deixa na Library do usuário (pelo id do pacote e pelo nome)."""
    lib = HOME / "Library"
    cands = []
    for base in ["Application Support", "Caches", "Containers", "Group Containers", "Saved Application State",
                 "HTTPStorages", "WebKit", "Logs"]:
        d = lib / base
        if not d.exists():
            continue
        for alvo in {bundle, app.stem, bundle + ".savedState" if bundle else ""} - {""}:
            if (d / alvo).exists():
                cands.append(d / alvo)
        if base == "Group Containers" and bundle:
            cands += list(d.glob("*." + bundle))
    return [p for p in dict.fromkeys(cands) if not any(str(p).startswith(str(i)) for i in INTOCAVEIS)]


def varrer_apps():
    itens = []
    for raiz in [Path("/Applications"), HOME / "Applications"]:
        for app in sorted(a for a in raiz.glob("*.app") if not a.name.startswith(".")) if raiz.exists() else []:
            bundle = sh(["mdls", "-raw", "-name", "kMDItemCFBundleIdentifier", str(app)])
            if bundle.startswith("com.apple."):
                continue
            bundle = "" if bundle == "(null)" else bundle
            uso = sh(["mdls", "-raw", "-name", "kMDItemLastUsedDate", str(app)])
            uso = "" if uso in ("", "(null)") else uso[:10]
            dias = None
            if uso:
                try:
                    dias = int((time.time() - time.mktime(time.strptime(uso, "%Y-%m-%d"))) // 86400)
                except Exception:
                    pass
            extras = dados_do_app(app, bundle)
            b_app, b_dados = tamanho(app), sum(tamanho(p) for p in extras)
            obs = ("Sem registro de uso" if dias is None else f"Último uso há {dias} dias ({uso})") + \
                  (f" · dados {b_dados / 2**30:.1f} GB" if b_dados > 50 * 2**20 else "")
            itens.append({"caminho": str(app), "nome": str(app).replace(str(HOME), "~"), "grupo": "Aplicativos",
                          "por_que": "Aplicativo e os dados dele na Library (vão juntos para a Lixeira).",
                          "nivel": "confirmar", "obs": obs, "bytes": b_app + b_dados, "tipo": "app",
                          "sem_uso": dias is None or dias > DIAS_SEM_USO, "extras": [str(p) for p in extras]})
    return itens


def rodar_varredura():
    with trava:
        if estado["varrendo"]:
            return
        estado["varrendo"] = True
    try:
        estado["itens"] = varrer()
        conhecidos.update({i["caminho"]: i for i in estado["itens"]})
        estado["erro"] = None
    except Exception as e:
        estado["erro"] = str(e)
    estado["atualizado"] = time.strftime("%H:%M:%S")
    estado["varrendo"] = False


def sistema():
    du = shutil.disk_usage("/")
    swap = sh(["sysctl", "-n", "vm.swapusage"])
    livre = sh(["memory_pressure"]).splitlines()[-1:] or [""]
    lix = tamanho(TRASH) if TRASH.exists() else 0
    procs = []
    for linha in sh(["ps", "-axo", "rss=,comm="]).splitlines():
        partes = linha.strip().split(None, 1)
        if len(partes) == 2 and partes[0].isdigit():
            procs.append((int(partes[0]) * 1024, os.path.basename(partes[1])))
    soma = {}
    for b, n in procs:
        soma[n] = soma.get(n, 0) + b
    top = sorted(soma.items(), key=lambda x: -x[1])[:10]
    return {"disco_total": du.total, "disco_usado": du.total - du.free, "disco_livre": du.free, "swap": swap,
            "memoria_livre": livre[0], "lixeira": lix, "processos": [{"nome": n, "bytes": b} for n, b in top]}


def registrar(acao, caminho, bytes_):
    with open(HIST, "a") as f:
        f.write(json.dumps({"quando": time.strftime("%Y-%m-%d %H:%M:%S"), "acao": acao, "caminho": caminho, "bytes": bytes_},
                           ensure_ascii=False) + "\n")


def para_lixeira(caminho):
    alvo = conhecidos.get(caminho)
    p = Path(caminho)
    if not alvo:
        return False, "item desconhecido; clique em Varrer de novo"
    if not p.exists():
        estado["itens"] = [i for i in estado["itens"] if i["caminho"] != caminho]
        return True, "já não existia"
    if "worktrees" in caminho and (p / ".git").exists():
        ok, obs = git_seguro(p)
        if not ok:
            return False, "bloqueado: " + obs
    if alvo.get("tipo") == "app":
        # O app vai pelo Finder (pede a senha do Mac quando o app é do administrador); os dados dele, direto.
        r = subprocess.run(["osascript", "-e", f'tell application "Finder" to delete (POSIX file "{caminho}")'],
                           capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            return False, "o Finder não moveu o app (feche o app e tente de novo)"
        for x in alvo.get("extras", []):
            if Path(x).exists():
                shutil.move(x, str(TRASH / (Path(x).name + "-" + time.strftime("%H%M%S"))))
        registrar("lixeira", caminho, alvo["bytes"])
        estado["itens"] = [i for i in estado["itens"] if i["caminho"] != caminho]
        return True, "app e dados movidos para a Lixeira"
    destino = TRASH / (p.name + "-" + time.strftime("%H%M%S"))
    shutil.move(str(p), str(destino))
    registrar("lixeira", caminho, alvo["bytes"])
    estado["itens"] = [i for i in estado["itens"] if i["caminho"] != caminho]
    return True, "movido para a Lixeira"


def esvaziar_lixeira():
    b = tamanho(TRASH)
    for f in TRASH.iterdir():
        try:
            shutil.rmtree(f) if f.is_dir() and not f.is_symlink() else f.unlink()
        except Exception:
            pass
    registrar("esvaziar_lixeira", str(TRASH), b)
    return True, "Lixeira esvaziada"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        d = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()
        self.wfile.write(d)

    def do_GET(self):
        if self.path == "/":
            d = (AQUI / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(d)
        elif self.path == "/api/estado":
            self._json({**estado, "sistema": sistema()})
        elif self.path == "/api/historico":
            linhas = HIST.read_text().splitlines() if HIST.exists() else []
            self._json([json.loads(l) for l in linhas[-50:]][::-1])
        else:
            self._json({"erro": "não encontrado"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/varrer":
            threading.Thread(target=rodar_varredura, daemon=True).start()
            self._json({"ok": True})
        elif self.path == "/api/lixeira":
            ok, msg = para_lixeira(body.get("caminho", ""))
            self._json({"ok": ok, "msg": msg}, 200 if ok else 400)
        elif self.path == "/api/esvaziar":
            ok, msg = esvaziar_lixeira()
            self._json({"ok": ok, "msg": msg})
        else:
            self._json({"erro": "não encontrado"}, 404)


if __name__ == "__main__":
    threading.Thread(target=rodar_varredura, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORTA), H)
    url = f"http://127.0.0.1:{PORTA}"
    print("Limpa Mac em", url, "(Ctrl+C para fechar)")
    webbrowser.open(url)
    srv.serve_forever()
