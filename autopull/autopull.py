#!/usr/bin/env python3
import os, sys, time, re, shlex, subprocess, pathlib, urllib.parse
from typing import List, Tuple, Set

def env(k, d=None): return os.getenv(k, d)

WORK_DIR = pathlib.Path(env("WORK_DIR", "/work")).resolve()
REPO_URL = env("GIT_URL", "").strip()
GIT_BRANCH = env("GIT_BRANCH", "main").strip()
GIT_LOGIN = env("GIT_LOGIN", "").strip()
GIT_TOKEN = env("GIT_TOKEN", "").strip()   # PAT (repo read)
POLL_INTERVAL = max(5, int(env("POLL_INTERVAL", "20")))
COMPOSE_FILE_PATH = pathlib.Path(env("COMPOSE_FILE_PATH", str(WORK_DIR / "docker-compose.yml"))).resolve()
DOCKER_COMPOSE_CMD = env("DOCKER_COMPOSE_CMD", "").strip()  # опционально: "docker compose" или "docker-compose"
AUTO_BUILD_ON_START = (env("AUTO_BUILD_ON_START", "0").lower() in {"1","true","yes"})
VERBOSE = (env("AUTOPULL_VERBOSE","1").lower() in {"1","true","yes"})

# маска для токена в логах
def mask(s: str) -> str:
    if not s: return s
    if len(s) <= 8: return "***"
    return f"{s[:4]}***{s[-4:]}"

def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[autopull] {ts} {msg}", flush=True)

def run_cmd(cmd: List[str], cwd: pathlib.Path=None, check=True) -> Tuple[int,str]:
    try:
        if VERBOSE:
            log(f"$ {' '.join(shlex.quote(c) for c in cmd)}")
        p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = p.stdout or ""
        return p.returncode, out
    except Exception as e:
        return 999, f"EXC: {e!r}"

def decide_compose_cmd() -> List[str]:
    # приоритет: указан через env
    if DOCKER_COMPOSE_CMD:
        parts = DOCKER_COMPOSE_CMD.split()
        return parts
    # пробуем docker compose (plugin)
    rc, _ = run_cmd(["docker","compose","version"])
    if rc == 0: return ["docker","compose"]
    # пробуем старый docker-compose
    rc, _ = run_cmd(["docker-compose","--version"])
    if rc == 0: return ["docker-compose"]
    log("ERROR: docker compose/ docker-compose не найден внутри контейнера")
    sys.exit(2)

COMPOSE_BASE = decide_compose_cmd()

def compose(args: List[str]) -> Tuple[int,str]:
    base = COMPOSE_BASE[:]
    if "compose" in " ".join(base):
        # docker compose
        base += ["-f", str(COMPOSE_FILE_PATH), "--project-directory", str(WORK_DIR)]
    else:
        # docker-compose
        base += ["-f", str(COMPOSE_FILE_PATH)]
    return run_cmd(base + args, cwd=WORK_DIR)

def embed_credentials(url: str, login: str, token: str) -> str:
    if not url: return url
    if not token and not login: return url
    u = urllib.parse.urlsplit(url)
    host = u.hostname or ""
    port = f":{u.port}" if u.port else ""
    if login and token:
        creds = f"{urllib.parse.quote(login)}:{urllib.parse.quote(token)}@"
    else:
        # токен в виде "https://TOKEN@github.com/user/repo.git"
        creds = f"{urllib.parse.quote(token)}@"
    new_netloc = creds + host + port
    return urllib.parse.urlunsplit((u.scheme, new_netloc, u.path, u.query, u.fragment))

def ensure_repo() -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    git_dir = WORK_DIR / ".git"
    if not REPO_URL:
        log("ERROR: GIT_URL не задан")
        sys.exit(2)

    url_with_auth = embed_credentials(REPO_URL, GIT_LOGIN, GIT_TOKEN)
    if not git_dir.exists():
        log(f"Клонирование репозитория {REPO_URL} (ветка {GIT_BRANCH})")
        rc, out = run_cmd(["git", "init"], cwd=WORK_DIR)
        if rc != 0: log(out); sys.exit(3)
        run_cmd(["git","remote","remove","origin"], cwd=WORK_DIR)
        rc, out = run_cmd(["git","remote","add","origin", url_with_auth], cwd=WORK_DIR)
        if rc != 0: log(out); sys.exit(3)
        rc, out = run_cmd(["git","fetch","origin", GIT_BRANCH, "--depth=50"], cwd=WORK_DIR)
        if rc != 0: log(out); sys.exit(3)
        rc, out = run_cmd(["git","checkout","-B", GIT_BRANCH, f"origin/{GIT_BRANCH}"], cwd=WORK_DIR)
        if rc != 0: log(out); sys.exit(3)
    else:
        # уже репо — просто убедимся в правильном origin
        url_with_auth = embed_credentials(REPO_URL, GIT_LOGIN, GIT_TOKEN)
        run_cmd(["git","remote","set-url","origin", url_with_auth], cwd=WORK_DIR)
        run_cmd(["git","fetch","origin","--prune"], cwd=WORK_DIR)

def rev_parse(ref: str) -> str:
    rc, out = run_cmd(["git","rev-parse", ref], cwd=WORK_DIR)
    return out.strip() if rc==0 else ""

def list_new_commits(old: str, new: str) -> List[str]:
    if not old or not new or old==new: return []
    rc, out = run_cmd(["git","rev-list", f"{old}..{new}"], cwd=WORK_DIR)
    if rc!=0: return []
    commits = [l.strip() for l in out.splitlines() if l.strip()]
    # вернуть от старых к новым
    return list(reversed(commits))

def commit_msg(commit: str) -> str:
    rc, out = run_cmd(["git","log","--format=%B","-n","1", commit], cwd=WORK_DIR)
    return out.strip() if rc==0 else ""

def changed_files(commit: str) -> Set[str]:
    rc, out = run_cmd(["git","diff-tree","--no-commit-id","--name-only","-r", commit], cwd=WORK_DIR)
    files = set()
    if rc==0:
        for l in out.splitlines():
            p = l.strip()
            if p: files.add(p)
    return files

DOCKER_PATTERNS = [
    re.compile(r"(^|/)docker-compose(\..*)?\.ya?ml$", re.I),
    re.compile(r"(^|/)docker_compose(\..*)?\.ya?ml$", re.I),
    re.compile(r"(^|/)compose(\..*)?\.ya?ml$", re.I),
    re.compile(r"(^|/)Dockerfile(\..*)?$", re.I),
    re.compile(r"(^|/)docker/.*", re.I),
]

def is_docker_related(path: str) -> bool:
    p = path.strip()
    for rx in DOCKER_PATTERNS:
        if rx.search(p): return True
    # дополнительный грубый признак
    return "docker" in p.lower()

def build_and_up(no_cache: bool=True):
    log("Начинаю полную пересборку и запуск (build + up -d)")
    args_build = ["build"]
    if no_cache: args_build.append("--no-cache")
    rc, out = compose(args_build)
    log(out)
    if rc!=0:
        log("ERROR: build завершился с ошибкой")
        return False
    rc, out = compose(["up","-d"])
    log(out)
    return (rc==0)

def restart_stack():
    log("Перезапуск сервисов через docker compose restart")
    rc, out = compose(["restart"])
    log(out)
    if rc!=0:
        # fallback: up -d
        log("restart вернул ошибку, пробую up -d")
        rc, out = compose(["up","-d"])
        log(out)
    return (rc==0)

def up_if_present():
    # полезно на старте, если сборка не требуется, но нужно поднять стек
    if COMPOSE_FILE_PATH.exists():
        rc, out = compose(["up","-d"])
        log(out)

def parse_flags_from_messages(msgs: List[str]) -> Tuple[bool,bool,int]:
    """Возвращает: (need_build, need_restart, reverse_n)"""
    need_build = False
    need_restart = False
    reverse_n = 0
    for m in msgs:
        txt = m.lower()
        if "--build" in txt: need_build = True
        if "--restart" in txt: need_restart = True
        m_rev = re.search(r"reverse\s*['\"]?\s*(\d+)\s*['\"]?", txt)
        if m_rev:
            try:
                reverse_n = max(reverse_n, int(m_rev.group(1)))
            except Exception:
                pass
    return need_build, need_restart, reverse_n

def main():
    log("Автопуллер стартует")
    if REPO_URL:
        log(f"Репозиторий: {REPO_URL}")
    if GIT_LOGIN or GIT_TOKEN:
        log(f"Auth: login={GIT_LOGIN or '-'} token={mask(GIT_TOKEN)}")
    log(f"Ветка: {GIT_BRANCH}, опрос каждые {POLL_INTERVAL}s")
    log(f"Compose: {COMPOSE_BASE} -f {COMPOSE_FILE_PATH}")

    ensure_repo()

    # при первом старте (опционально) просто поднять стек
    if AUTO_BUILD_ON_START and COMPOSE_FILE_PATH.exists():
        build_and_up(no_cache=False)
    elif COMPOSE_FILE_PATH.exists():
        up_if_present()

    last_local = rev_parse("HEAD")
    while True:
        try:
            # fetch + список ревизий
            run_cmd(["git","fetch","origin","--prune"], cwd=WORK_DIR)
            remote_head = rev_parse(f"origin/{GIT_BRANCH}")
            local_head = rev_parse("HEAD")

            if remote_head and local_head and remote_head != local_head:
                log(f"Обнаружены новые коммиты: {local_head[:7]}..{remote_head[:7]}")
                # собрать список коммитов ДО merge
                new_commits = list_new_commits(local_head, remote_head)

                # тянем изменения
                rc, out = run_cmd(["git","pull","--rebase","origin", GIT_BRANCH], cwd=WORK_DIR)
                log(out)
                if rc != 0:
                    log("WARN: pull завершился с ошибкой, пробую hard reset на remote_head")
                    run_cmd(["git","reset","--hard", remote_head], cwd=WORK_DIR)

                # перечитаем HEAD
                local_head = rev_parse("HEAD")

                # флаги из сообщений
                messages = [commit_msg(c) for c in new_commits] if new_commits else [commit_msg(local_head)]
                need_build, need_restart, reverse_n = parse_flags_from_messages(messages)

                # посмотрим изменённые файлы
                changed: Set[str] = set()
                for c in new_commits or [local_head]:
                    changed |= changed_files(c)

                docker_changed = any(is_docker_related(p) for p in changed)
                if docker_changed:
                    log(f"Обнаружены docker-изменения: {sorted(changed)}")

                # reverse N
                if reverse_n > 0:
                    log(f"Запрошен откат reverse {reverse_n}: git reset --hard HEAD~{reverse_n}")
                    rc, out = run_cmd(["git","reset","--hard", f"HEAD~{reverse_n}"], cwd=WORK_DIR)
                    log(out)
                    build_and_up(no_cache=True)
                else:
                    # действия по флагам/изменениям
                    if need_build or docker_changed:
                        build_and_up(no_cache=True)
                    elif need_restart:
                        restart_stack()
                    else:
                        log("Новые коммиты без специальных флагов — действий не требуется")

                last_local = local_head

            # sleep
            time.sleep(POLL_INTERVAL)

        except Exception as e:
            log(f"ERROR loop: {e!r}")
            time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    # проверим сокет докера (для понятного сообщения)
    if not pathlib.Path("/var/run/docker.sock").exists():
        log("⚠️  /var/run/docker.sock не смонтирован — docker команды работать не будут")
    try:
        main()
    except KeyboardInterrupt:
        log("Завершение по Ctrl-C")
