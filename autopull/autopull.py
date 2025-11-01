#!/usr/bin/env python3
import os, sys, time, re, shlex, subprocess, pathlib, urllib.parse
from typing import List, Tuple, Set

# ----------------------- БАЗОВЫЕ УТИЛИТЫ -----------------------

def env(k, d=None):
    return os.getenv(k, d)

# Базовые настройки
WORK_DIR = pathlib.Path(env("WORK_DIR", "/work")).resolve()
REPO_URL = env("GIT_URL", "").strip()
GIT_BRANCH = env("GIT_BRANCH", "main").strip()
GIT_LOGIN = env("GIT_LOGIN", "").strip()
GIT_TOKEN = env("GIT_TOKEN", "").strip()
POLL_INTERVAL = max(5, int(env("POLL_INTERVAL", "20")))
COMPOSE_FILE_PATH = pathlib.Path(env("COMPOSE_FILE_PATH", str(WORK_DIR / "docker-compose.yml"))).resolve()
AUTOPULL_VERBOSE = (env("AUTOPULL_VERBOSE","1").lower() in {"1","true","yes"})
AUTOPULL_COLOR   = (env("AUTOPULL_COLOR","1").lower() in {"1","true","yes"})
AUTO_BUILD_ON_START = (env("AUTO_BUILD_ON_START","0").lower() in {"1","true","yes"})
DOCKER_COMPOSE_CMD = env("DOCKER_COMPOSE_CMD", "").strip()

# Новые/усиленные параметры
ALWAYS_REBUILD_ON_COMMIT = (env("ALWAYS_REBUILD_ON_COMMIT","1").lower() in {"1","true","yes"})
AUTOPULL_RETRIES = max(1, int(env("AUTOPULL_RETRIES", "2")))      # сколько раз повторять неудачные compose-команды
RETRY_SLEEP_BASE = max(1, int(env("RETRY_SLEEP_BASE", "2")))      # базовая пауза между ретраями

# Цвета
if AUTOPULL_COLOR:
    C = {
        "reset":"\033[0m", "dim":"\033[2m",
        "cyan":"\033[36m", "green":"\033[32m",
        "yellow":"\033[33m", "red":"\033[31m",
        "magenta":"\033[35m"
    }
else:
    C = {k:"" for k in ["reset","dim","cyan","green","yellow","red","magenta"]}

def mask(s: str) -> str:
    if not s: return s
    return f"{s[:4]}***{s[-4:]}" if len(s) > 8 else "***"

def log(msg: str, lvl: str="info"):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    color = {"info":C["reset"], "ok":C["green"], "warn":C["yellow"], "err":C["red"], "cmd":C["cyan"], "dim":C["dim"]}.get(lvl, C["reset"])
    print(f"{color}[autopull] {ts} {msg}{C['reset']}", flush=True)

def run_cmd(cmd: List[str], cwd: pathlib.Path=None) -> Tuple[int,str]:
    if AUTOPULL_VERBOSE:
        log("$ " + " ".join(shlex.quote(c) for c in cmd), "cmd")
    try:
        p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return p.returncode, p.stdout or ""
    except Exception as e:
        return 999, f"EXC: {e!r}"

# ----------------------- COMPOSE ДЕТЕКТ/ОБВЁРТКИ -----------------------

def decide_compose_cmd() -> List[str]:
    """
    Возвращаем корректную «базу» для docker compose / docker-compose / podman compose.
    """
    if DOCKER_COMPOSE_CMD:
        return DOCKER_COMPOSE_CMD.split()

    # Предпочтение современному docker compose
    rc, _ = run_cmd(["docker", "compose", "version"])
    if rc == 0:
        return ["docker", "compose"]

    # Legacy docker-compose
    rc, _ = run_cmd(["docker-compose", "--version"])
    if rc == 0:
        return ["docker-compose"]

    # Иногда бывает podman compose
    rc, _ = run_cmd(["podman", "compose", "version"])
    if rc == 0:
        return ["podman", "compose"]

    log("docker compose/docker-compose не найден внутри контейнера", "err")
    sys.exit(2)

COMPOSE_BASE = decide_compose_cmd()

def _is_v2_like(base: List[str]) -> bool:
    """True для ['docker','compose'] или ['podman','compose']"""
    return len(base) >= 2 and base[1] == "compose"

def _is_legacy(base: List[str]) -> bool:
    """True для ['docker-compose']"""
    return len(base) == 1 and base[0].endswith("docker-compose")

def compose(args: List[str]) -> Tuple[int,str]:
    """
    Единая точка вызова docker compose / docker-compose.
    ВАЖНО: project-directory = директория, где лежит COMPOSE_FILE_PATH, чтобы чинить относительные пути (env_file и т.п.).
    """
    base = COMPOSE_BASE[:]
    project_dir = COMPOSE_FILE_PATH.parent

    if _is_v2_like(base):
        # docker compose / podman compose
        base += ["-f", str(COMPOSE_FILE_PATH), "--project-directory", str(project_dir)]
    elif _is_legacy(base):
        # legacy docker-compose
        os.environ.setdefault("COMPOSE_FILE", str(COMPOSE_FILE_PATH))
        base += ["-f", str(COMPOSE_FILE_PATH)]
    else:
        # На всякий — fallback как у legacy
        os.environ.setdefault("COMPOSE_FILE", str(COMPOSE_FILE_PATH))
        base += ["-f", str(COMPOSE_FILE_PATH)]

    return run_cmd(base + args, cwd=project_dir)

def compose_safe(args: List[str], retries: int = AUTOPULL_RETRIES) -> Tuple[int, str]:
    """
    Повторяем команду с небольшими паузами, чтобы сетевые/реестровые глюки не валили процесс.
    """
    last_rc, last_out = 1, ""
    for attempt in range(1, retries + 1):
        rc, out = compose(args)
        if rc == 0:
            return rc, out
        last_rc, last_out = rc, out
        log(f"Команда {' '.join(args)} завершилась rc={rc}. Попытка {attempt}/{retries}.", "warn")
        time.sleep(RETRY_SLEEP_BASE * attempt)
    return last_rc, last_out

def compose_validate() -> bool:
    rc, out = compose(["config"])
    if rc != 0:
        log("docker compose config вернул ошибку — проверьте compose-файл ниже:", "err")
        log(out, "dim")
        return False
    return True

# ----------------------- GIT УТИЛИТЫ -----------------------

def embed_credentials(url: str, login: str, token: str) -> str:
    if not url: return url
    if not token and not login: return url
    u = urllib.parse.urlsplit(url)
    host = u.hostname or ""
    port = f":{u.port}" if u.port else ""
    if login and token:
        creds = f"{urllib.parse.quote(login)}:{urllib.parse.quote(token)}@"
    else:
        # Используем токен как логин — популярный паттерн для GitHub/ГитLab PAT
        creds = f"{urllib.parse.quote(token)}@"
    return urllib.parse.urlunsplit((u.scheme, creds + host + port, u.path, u.query, u.fragment))

def ensure_repo() -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    git_dir = WORK_DIR / ".git"
    if not REPO_URL:
        log("GIT_URL не задан", "err")
        sys.exit(2)

    url_with_auth = embed_credentials(REPO_URL, GIT_LOGIN, GIT_TOKEN)
    if not git_dir.exists():
        log(f"Клонирую {REPO_URL} (ветка {GIT_BRANCH})", "info")
        rc, out = run_cmd(["git", "init"], cwd=WORK_DIR);                  log(out, "dim")
        run_cmd(["git","remote","remove","origin"], cwd=WORK_DIR)
        rc, out = run_cmd(["git","remote","add","origin", url_with_auth], cwd=WORK_DIR); log(out, "dim")
        rc, out = run_cmd(["git","fetch","origin", GIT_BRANCH, "--depth=50"], cwd=WORK_DIR); log(out, "dim")
        rc, out = run_cmd(["git","checkout","-B", GIT_BRANCH, f"origin/{GIT_BRANCH}"], cwd=WORK_DIR); log(out, "dim")
    else:
        run_cmd(["git","remote","set-url","origin", url_with_auth], cwd=WORK_DIR)
        run_cmd(["git","fetch","origin","--prune"], cwd=WORK_DIR)

def rev_parse(ref: str) -> str:
    rc, out = run_cmd(["git","rev-parse", ref], cwd=WORK_DIR)
    return out.strip() if rc==0 else ""

def list_new_commits(old: str, new: str) -> List[str]:
    if not old or not new or old==new: return []
    rc, out = run_cmd(["git","rev-list", f"{old}..{new}"], cwd=WORK_DIR)
    commits = [l.strip() for l in out.splitlines() if l.strip()] if rc==0 else []
    return list(reversed(commits))

def commit_msg(commit: str) -> str:
    rc, out = run_cmd(["git","log","--format=%B","-n","1", commit], cwd=WORK_DIR)
    return out.strip() if rc==0 else ""

def changed_files(commit: str) -> Set[str]:
    rc, out = run_cmd(["git","diff-tree","--no-commit-id","--name-only","-r", commit], cwd=WORK_DIR)
    return {l.strip() for l in (out.splitlines() if rc==0 else []) if l.strip()}

# ----------------------- ЛОГИКА ИЗМЕНЕНИЙ -----------------------

DOCKER_PATTERNS = [
    re.compile(r"(^|/)docker-compose(\..*)?\.ya?ml$", re.I),
    re.compile(r"(^|/)docker_compose(\..*)?\.ya?ml$", re.I),
    re.compile(r"(^|/)compose(\..*)?\.ya?ml$", re.I),
    re.compile(r"(^|/)Dockerfile(\..*)?$", re.I),
    re.compile(r"(^|/)docker/.*", re.I),
]

def is_docker_related(path: str) -> bool:
    if any(rx.search(path) for rx in DOCKER_PATTERNS):
        return True
    # Простая эвристика
    return "docker" in path.lower() or path.lower().endswith(".env")

def parse_flags_from_messages(msgs: List[str]) -> Tuple[bool,bool,int]:
    need_build = False; need_restart = False; reverse_n = 0
    for m in msgs:
        txt = m.lower()
        if "--build" in txt: need_build = True
        if "--restart" in txt: need_restart = True
        m_rev = re.search(r"reverse\s*['\"]?\s*(\d+)\s*['\"]?", txt)
        if m_rev:
            try: reverse_n = max(reverse_n, int(m_rev.group(1)))
            except: pass
    return need_build, need_restart, reverse_n

# ----------------------- ДЕЙСТВИЯ С СТЕКОМ -----------------------

def hard_update(no_cache: bool=False) -> bool:
    """
    Тяжёлый путь для docker-изменений:
    - docker compose config (валидация)
    - pull
    - build [--pull] [--no-cache]
    - up -d --remove-orphans
    """
    if not COMPOSE_FILE_PATH.exists():
        log(f"Compose-файл не найден по пути: {COMPOSE_FILE_PATH}", "err")
        return False

    if not compose_validate():
        return False

    log("Тяну образы (compose pull)", "info")
    rc, out = compose_safe(["pull"]); log(out, "dim")
    if rc != 0:
        log("pull завершился с ошибкой — продолжаю (может быть локальная сборка).", "warn")

    build_args = ["build", "--pull"] + (["--no-cache"] if no_cache else [])
    log(f"Собираю сервисы ({'без кеша, ' if no_cache else ''}compose build)", "ok")
    rc, out = compose_safe(build_args); log(out, "dim")
    if rc != 0:
        log("build завершился с ошибкой", "err")
        return False

    log("Поднимаю стек (up -d --remove-orphans)", "ok")
    rc, out = compose_safe(["up","-d","--remove-orphans"]); log(out, "dim")
    return (rc == 0)

def light_update() -> bool:
    """
    Лёгкий путь для обычных коммитов: up -d --build (быстро применяет изменения).
    """
    if not COMPOSE_FILE_PATH.exists():
        log(f"Compose-файл не найден по пути: {COMPOSE_FILE_PATH}", "err")
        return False

    if not compose_validate():
        return False

    log("Применяю изменения (up -d --build)", "ok")
    rc, out = compose_safe(["up","-d","--build"]); log(out, "dim")
    return (rc == 0)

def restart_stack() -> bool:
    log("Перезапуск сервисов (compose restart)", "ok")
    rc, out = compose_safe(["restart"]); log(out, "dim")
    if rc != 0:
        log("restart вернул ошибку, пробую up -d", "warn")
        rc, out = compose_safe(["up","-d"]); log(out, "dim")
    return (rc == 0)

def up_if_present():
    if COMPOSE_FILE_PATH.exists():
        compose_validate()
        rc, out = compose_safe(["up","-d"]); log(out, "dim")

# ----------------------- ОСНОВНОЙ ЦИКЛ -----------------------

def main():
    log(f"Старт: repo={REPO_URL} branch={GIT_BRANCH}", "info")
    if GIT_LOGIN or GIT_TOKEN:
        log(f"Auth: login={GIT_LOGIN or '-'} token={mask(GIT_TOKEN)}", "info")
    log(f"Compose base: {' '.join(COMPOSE_BASE)} -f {COMPOSE_FILE_PATH}", "info")

    ensure_repo()

    if AUTO_BUILD_ON_START and COMPOSE_FILE_PATH.exists():
        # На старте поднимаем с билдом, но без --no-cache
        light_update()
    elif COMPOSE_FILE_PATH.exists():
        up_if_present()

    local_head = rev_parse("HEAD")
    while True:
        try:
            run_cmd(["git","fetch","origin","--prune"], cwd=WORK_DIR)
            remote_head = rev_parse(f"origin/{GIT_BRANCH}")
            local_head = rev_parse("HEAD")

            if remote_head and local_head and remote_head != local_head:
                log(f"Новые коммиты: {local_head[:7]}..{remote_head[:7]}", "info")
                new_commits = list_new_commits(local_head, remote_head)

                rc, out = run_cmd(["git","pull","--rebase","origin", GIT_BRANCH], cwd=WORK_DIR); log(out, "dim")
                if rc != 0:
                    log("pull упал, делаю reset --hard на remote_head и clean -df", "warn")
                    run_cmd(["git","reset","--hard", remote_head], cwd=WORK_DIR)
                    run_cmd(["git","clean","-df"], cwd=WORK_DIR)

                local_head = rev_parse("HEAD")

                messages = [commit_msg(c) for c in new_commits] if new_commits else [commit_msg(local_head)]
                need_build, need_restart, reverse_n = parse_flags_from_messages(messages)

                changed: Set[str] = set()
                for c in new_commits or [local_head]:
                    changed |= changed_files(c)

                docker_changed = any(is_docker_related(p) for p in changed)
                if docker_changed:
                    log(f"Изменены docker/compose-файлы: {sorted(changed)}", "info")

                # Обработка reverse
                if reverse_n > 0:
                    log(f"Откат reverse {reverse_n}: git reset --hard HEAD~{reverse_n}", "warn")
                    rc, out = run_cmd(["git","reset","--hard", f"HEAD~{reverse_n}"], cwd=WORK_DIR); log(out, "dim")
                    hard_update(no_cache=True)
                else:
                    # Приоритет действий: docker-изменения → build; иначе — общий апдейт
                    if docker_changed:
                        ok = hard_update(no_cache=True)
                        if not ok:
                            log("Тяжёлый апдейт не удался, пробую лёгкий путь.", "warn")
                            light_update()
                    else:
                        if need_build or ALWAYS_REBUILD_ON_COMMIT:
                            light_update()
                        elif need_restart:
                            restart_stack()
                        else:
                            log("Коммиты без спец-флагов — применяю по умолчанию up -d --build", "info")
                            light_update()

            time.sleep(POLL_INTERVAL)

        except Exception as e:
            log(f"ERROR loop: {e!r}", "err")
            time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    if not pathlib.Path("/var/run/docker.sock").exists():
        log("⚠ /var/run/docker.sock не смонтирован — docker-команды не будут работать", "warn")
    try:
        main()
    except KeyboardInterrupt:
        log("Завершение по Ctrl-C", "info")
