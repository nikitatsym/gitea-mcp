# Gitea MCP Server — План реализации

## Архитектура

Повторяем паттерн ticktick-mcp:
- **Build system:** hatchling
- **Зависимости:** `mcp>=1.0.0` (FastMCP), плюс `httpx` для HTTP
- **Транспорт:** stdio
- **Точка входа:** `gitea-mcp` → `gitea_mcp:main`
- **Структура:**

```
gitea-mcp/
├── pyproject.toml
├── .github/workflows/build.yml
├── docs/index.html                    # GitHub Pages — генератор конфига
├── src/gitea_mcp/
│   ├── __init__.py                    # main()
│   ├── __main__.py                    # python -m gitea_mcp
│   ├── server.py                      # FastMCP + все тулы
│   └── client.py                      # GiteaClient (httpx)
├── tests/
│   ├── conftest.py                    # pytest fixtures, docker compose up/down
│   ├── docker-compose.yml             # Gitea instance
│   └── test_integration.py            # Интеграционные тесты
└── README.md
```

## Env-переменные

| Переменная | Описание |
|---|---|
| `GITEA_URL` | URL инстанса (например `https://gitea.example.com`) |
| `GITEA_TOKEN` | API токен (Personal Access Token) |

Авторизация только по токену — OAuth не нужен, Gitea поддерживает PAT из коробки.

## HTTP-клиент

`GiteaClient` на базе `httpx` (sync):
- Base URL из `GITEA_URL`
- Header `Authorization: token {GITEA_TOKEN}`
- Методы: `get`, `post`, `put`, `patch`, `delete`
- Пагинация: автоматический обход `?page=N&limit=50`
- Обработка ошибок: исключения с HTTP status + body

---

## Список MCP Tools (156 штук)

### Общее (2)

| # | Tool | Описание |
|---|------|----------|
| 1 | `get_version` | Получить версию Gitea API |
| 2 | `get_current_user` | Получить информацию о текущем пользователе |

### Пользователи (10)

| # | Tool | Описание |
|---|------|----------|
| 3 | `search_users` | Поиск пользователей |
| 4 | `get_user` | Получить профиль пользователя по username |
| 5 | `list_user_repos` | Список репозиториев пользователя |
| 6 | `list_followers` | Список подписчиков |
| 7 | `list_following` | Список подписок |
| 8 | `follow_user` | Подписаться на пользователя |
| 9 | `unfollow_user` | Отписаться от пользователя |
| 10 | `list_user_heatmap` | Тепловая карта активности пользователя |
| 11 | `get_user_settings` | Получить настройки текущего пользователя |
| 12 | `update_user_settings` | Обновить настройки текущего пользователя |

### SSH/GPG ключи (6)

| # | Tool | Описание |
|---|------|----------|
| 13 | `list_ssh_keys` | Список SSH-ключей текущего пользователя |
| 14 | `create_ssh_key` | Добавить SSH-ключ |
| 15 | `delete_ssh_key` | Удалить SSH-ключ |
| 16 | `list_gpg_keys` | Список GPG-ключей текущего пользователя |
| 17 | `create_gpg_key` | Добавить GPG-ключ |
| 18 | `delete_gpg_key` | Удалить GPG-ключ |

### Репозитории (14)

| # | Tool | Описание |
|---|------|----------|
| 19 | `search_repos` | Поиск репозиториев |
| 20 | `create_repo` | Создать репозиторий |
| 21 | `get_repo` | Получить информацию о репозитории |
| 22 | `edit_repo` | Редактировать репозиторий |
| 23 | `delete_repo` | Удалить репозиторий |
| 24 | `fork_repo` | Форкнуть репозиторий |
| 25 | `list_forks` | Список форков |
| 26 | `list_repo_topics` | Список тем репозитория |
| 27 | `set_repo_topics` | Установить темы репозитория |
| 28 | `list_repo_collaborators` | Список коллабораторов |
| 29 | `add_repo_collaborator` | Добавить коллаборатора |
| 30 | `remove_repo_collaborator` | Удалить коллаборатора |
| 31 | `star_repo` | Поставить звезду |
| 32 | `unstar_repo` | Убрать звезду |

### Вебхуки (5)

| # | Tool | Описание |
|---|------|----------|
| 33 | `list_repo_webhooks` | Список вебхуков репозитория |
| 34 | `create_repo_webhook` | Создать вебхук |
| 35 | `edit_repo_webhook` | Редактировать вебхук |
| 36 | `delete_repo_webhook` | Удалить вебхук |
| 37 | `test_repo_webhook` | Тестировать вебхук |

### Deploy Keys (3)

| # | Tool | Описание |
|---|------|----------|
| 38 | `list_deploy_keys` | Список deploy-ключей |
| 39 | `create_deploy_key` | Добавить deploy-ключ |
| 40 | `delete_deploy_key` | Удалить deploy-ключ |

### Файлы и контент (6)

| # | Tool | Описание |
|---|------|----------|
| 41 | `get_file_content` | Получить содержимое файла |
| 42 | `create_file` | Создать файл в репозитории |
| 43 | `update_file` | Обновить файл в репозитории |
| 44 | `delete_file` | Удалить файл из репозитория |
| 45 | `get_directory_content` | Получить содержимое директории |
| 46 | `get_raw_file` | Получить сырое содержимое файла |

### Ветки (7)

| # | Tool | Описание |
|---|------|----------|
| 47 | `list_branches` | Список веток |
| 48 | `get_branch` | Получить информацию о ветке |
| 49 | `create_branch` | Создать ветку |
| 50 | `delete_branch` | Удалить ветку |
| 51 | `list_branch_protections` | Список правил защиты веток |
| 52 | `create_branch_protection` | Создать правило защиты ветки |
| 53 | `delete_branch_protection` | Удалить правило защиты ветки |

### Коммиты и статусы (7)

| # | Tool | Описание |
|---|------|----------|
| 54 | `list_commits` | Список коммитов |
| 55 | `get_commit` | Получить информацию о коммите |
| 56 | `get_commit_diff` | Получить diff коммита |
| 57 | `compare_commits` | Сравнить два коммита/ветки |
| 58 | `list_commit_statuses` | Список статусов коммита |
| 59 | `create_commit_status` | Создать статус коммита |
| 60 | `get_combined_commit_status` | Получить объединённый статус коммита |

### Теги и релизы (8)

| # | Tool | Описание |
|---|------|----------|
| 61 | `list_tags` | Список тегов |
| 62 | `create_tag` | Создать тег |
| 63 | `delete_tag` | Удалить тег |
| 64 | `list_releases` | Список релизов |
| 65 | `get_release` | Получить релиз |
| 66 | `create_release` | Создать релиз |
| 67 | `edit_release` | Редактировать релиз |
| 68 | `delete_release` | Удалить релиз |

### Labels (4)

| # | Tool | Описание |
|---|------|----------|
| 69 | `list_repo_labels` | Список меток репозитория |
| 70 | `create_repo_label` | Создать метку |
| 71 | `edit_repo_label` | Редактировать метку |
| 72 | `delete_repo_label` | Удалить метку |

### Milestones (5)

| # | Tool | Описание |
|---|------|----------|
| 73 | `list_milestones` | Список вех |
| 74 | `get_milestone` | Получить веху |
| 75 | `create_milestone` | Создать веху |
| 76 | `edit_milestone` | Редактировать веху |
| 77 | `delete_milestone` | Удалить веху |

### Issues (14)

| # | Tool | Описание |
|---|------|----------|
| 78 | `list_issues` | Список issues |
| 79 | `search_issues` | Поиск issues по всем репозиториям |
| 80 | `get_issue` | Получить issue |
| 81 | `create_issue` | Создать issue |
| 82 | `edit_issue` | Редактировать issue (title, body, state, assignees, milestone, labels) |
| 83 | `list_issue_comments` | Список комментариев к issue |
| 84 | `create_issue_comment` | Создать комментарий к issue |
| 85 | `edit_issue_comment` | Редактировать комментарий |
| 86 | `delete_issue_comment` | Удалить комментарий |
| 87 | `list_issue_labels` | Список меток issue |
| 88 | `add_issue_labels` | Добавить метки к issue |
| 89 | `remove_issue_label` | Удалить метку с issue |
| 90 | `replace_issue_labels` | Заменить все метки issue |
| 91 | `set_issue_deadline` | Установить дедлайн issue |

### Issue — расширенное (10)

| # | Tool | Описание |
|---|------|----------|
| 92 | `list_issue_dependencies` | Список зависимостей issue |
| 93 | `add_issue_dependency` | Добавить зависимость |
| 94 | `remove_issue_dependency` | Удалить зависимость |
| 95 | `pin_issue` | Закрепить issue |
| 96 | `unpin_issue` | Открепить issue |
| 97 | `lock_issue` | Заблокировать обсуждение |
| 98 | `unlock_issue` | Разблокировать обсуждение |
| 99 | `list_issue_subscriptions` | Список подписчиков issue |
| 100 | `subscribe_to_issue` | Подписаться на issue |
| 101 | `unsubscribe_from_issue` | Отписаться от issue |

### Reactions (6)

| # | Tool | Описание |
|---|------|----------|
| 102 | `list_issue_reactions` | Список реакций к issue |
| 103 | `add_issue_reaction` | Добавить реакцию к issue |
| 104 | `remove_issue_reaction` | Удалить реакцию с issue |
| 105 | `list_comment_reactions` | Список реакций к комментарию |
| 106 | `add_comment_reaction` | Добавить реакцию к комментарию |
| 107 | `remove_comment_reaction` | Удалить реакцию с комментария |

### Time Tracking (5)

| # | Tool | Описание |
|---|------|----------|
| 108 | `list_tracked_times` | Список затраченного времени |
| 109 | `add_tracked_time` | Добавить затраченное время |
| 110 | `delete_tracked_time` | Удалить запись о времени |
| 111 | `start_stopwatch` | Запустить секундомер |
| 112 | `stop_stopwatch` | Остановить секундомер |

### Pull Requests (14)

| # | Tool | Описание |
|---|------|----------|
| 113 | `list_pull_requests` | Список PR |
| 114 | `get_pull_request` | Получить PR |
| 115 | `create_pull_request` | Создать PR |
| 116 | `edit_pull_request` | Редактировать PR |
| 117 | `merge_pull_request` | Замержить PR |
| 118 | `get_pull_request_diff` | Получить diff PR |
| 119 | `get_pull_request_files` | Получить файлы изменённые в PR |
| 120 | `get_pull_request_commits` | Получить коммиты PR |
| 121 | `update_pull_request_branch` | Обновить ветку PR (rebase/merge from base) |
| 122 | `list_pull_reviews` | Список ревью |
| 123 | `create_pull_review` | Создать ревью |
| 124 | `submit_pull_review` | Отправить pending ревью |
| 125 | `request_pull_reviewers` | Запросить ревью у пользователей |
| 126 | `dismiss_pull_review` | Отклонить ревью |

### Actions / CI (15)

| # | Tool | Описание |
|---|------|----------|
| 127 | `list_workflows` | Список workflows |
| 128 | `get_workflow` | Получить workflow |
| 129 | `dispatch_workflow` | Запустить workflow (workflow_dispatch) |
| 130 | `get_workflow_run` | Получить информацию о запуске workflow |
| 131 | `list_workflow_run_jobs` | Список job-ов в запуске |
| 132 | `get_workflow_job` | Получить информацию о job |
| 133 | `get_workflow_job_logs` | Получить логи job |
| 134 | `list_action_secrets` | Список секретов |
| 135 | `create_action_secret` | Создать/обновить секрет |
| 136 | `delete_action_secret` | Удалить секрет |
| 137 | `list_action_variables` | Список переменных |
| 138 | `get_action_variable` | Получить переменную |
| 139 | `create_action_variable` | Создать переменную |
| 140 | `update_action_variable` | Обновить переменную |
| 141 | `delete_action_variable` | Удалить переменную |

### Организации (7)

| # | Tool | Описание |
|---|------|----------|
| 142 | `list_orgs` | Список организаций текущего пользователя |
| 143 | `get_org` | Получить информацию об организации |
| 144 | `create_org` | Создать организацию |
| 145 | `edit_org` | Редактировать организацию |
| 146 | `delete_org` | Удалить организацию |
| 147 | `list_org_repos` | Список репозиториев организации |
| 148 | `list_org_members` | Список участников организации |

### Команды (Teams) (10)

| # | Tool | Описание |
|---|------|----------|
| 149 | `list_org_teams` | Список команд организации |
| 150 | `get_team` | Получить информацию о команде |
| 151 | `create_team` | Создать команду |
| 152 | `edit_team` | Редактировать команду |
| 153 | `delete_team` | Удалить команду |
| 154 | `list_team_members` | Список участников команды |
| 155 | `add_team_member` | Добавить участника в команду |
| 156 | `remove_team_member` | Удалить участника из команды |
| 157 | `list_team_repos` | Список репозиториев команды |
| 158 | `add_team_repo` | Добавить репозиторий в команду |

### Org Labels (4)

| # | Tool | Описание |
|---|------|----------|
| 159 | `list_org_labels` | Список меток организации |
| 160 | `create_org_label` | Создать метку организации |
| 161 | `edit_org_label` | Редактировать метку организации |
| 162 | `delete_org_label` | Удалить метку организации |

### Notifications (4)

| # | Tool | Описание |
|---|------|----------|
| 163 | `list_notifications` | Список уведомлений |
| 164 | `mark_notifications_read` | Отметить все уведомления прочитанными |
| 165 | `get_notification_thread` | Получить тред уведомления |
| 166 | `mark_notification_read` | Отметить уведомление прочитанным |

### Wiki (5)

| # | Tool | Описание |
|---|------|----------|
| 167 | `list_wiki_pages` | Список страниц wiki |
| 168 | `get_wiki_page` | Получить страницу wiki |
| 169 | `create_wiki_page` | Создать страницу wiki |
| 170 | `edit_wiki_page` | Редактировать страницу wiki |
| 171 | `delete_wiki_page` | Удалить страницу wiki |

### Packages (4)

| # | Tool | Описание |
|---|------|----------|
| 172 | `list_packages` | Список пакетов |
| 173 | `get_package` | Получить информацию о пакете |
| 174 | `delete_package` | Удалить пакет |
| 175 | `list_package_files` | Список файлов пакета |

### Admin (7)

| # | Tool | Описание |
|---|------|----------|
| 176 | `admin_list_users` | Список всех пользователей (admin) |
| 177 | `admin_create_user` | Создать пользователя (admin) |
| 178 | `admin_edit_user` | Редактировать пользователя (admin) |
| 179 | `admin_delete_user` | Удалить пользователя (admin) |
| 180 | `admin_list_orgs` | Список всех организаций (admin) |
| 181 | `admin_list_cron_jobs` | Список cron-задач (admin) |
| 182 | `admin_run_cron_job` | Запустить cron-задачу (admin) |

### Misc (4)

| # | Tool | Описание |
|---|------|----------|
| 183 | `render_markdown` | Рендерить markdown в HTML |
| 184 | `search_topics` | Поиск тем |
| 185 | `list_gitignore_templates` | Список шаблонов .gitignore |
| 186 | `list_license_templates` | Список шаблонов лицензий |

---

**Итого: 186 тулов**

---

## Docker Compose тесты

### docker-compose.yml

```yaml
services:
  gitea:
    image: gitea/gitea:latest
    environment:
      - GITEA__security__INSTALL_LOCK=true
      - GITEA__server__ROOT_URL=http://localhost:3000
      - GITEA__actions__ENABLED=true
      - GITEA__actions__DEFAULT_ACTIONS_URL=https://github.com
    ports:
      - "3000:3000"
    volumes:
      - gitea-data:/data

  act_runner:
    image: gitea/act_runner:latest
    depends_on:
      - gitea
    environment:
      - GITEA_INSTANCE_URL=http://gitea:3000
      - GITEA_RUNNER_REGISTRATION_TOKEN=<будет генерироваться>
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock

volumes:
  gitea-data:
```

### conftest.py — Что делает

1. `docker compose up -d` — поднимает Gitea
2. Ждёт готовности Gitea (poll `GET /api/v1/version`)
3. Через API создаёт admin-пользователя (первый пользователь = admin)
4. Генерирует PAT (API token)
5. Регистрирует act_runner для выполнения Actions
6. Создаёт тестовый репозиторий с workflow-файлом
7. Предоставляет фикстуру `gitea_client` с рабочим токеном
8. После тестов — `docker compose down -v`

### Что тестируем

1. **Репозитории** — create, get, edit, search, delete, fork, topics, collaborators
2. **Файлы** — create_file, get_file_content, update_file, delete_file, directory, raw
3. **Ветки** — create, list, get, delete, branch protection
4. **Коммиты** — list, get, diff, compare, statuses
5. **Issues** — full CRUD + comments + labels + milestones + dependencies + pin/lock + reactions + time tracking
6. **Pull Requests** — create (из ветки), get, list, files, diff, merge, reviews
7. **Actions** — list_workflows, dispatch_workflow, get_run, list_jobs, get_job_logs, secrets CRUD, variables CRUD
8. **Организации** — create, get, list, repos, members, teams CRUD
9. **Теги/релизы** — create, list, get, edit, delete
10. **Wiki** — create, get, list, edit, delete
11. **Notifications** — list, mark read
12. **Admin** — list users, create user
13. **Webhooks** — create, list, delete
14. **Misc** — render markdown, search topics

### Порядок тестов

Тесты идут последовательно с зависимостями через тестовые данные:
1. Create repo → 2. Create files → 3. Create branch → 4. Create file in branch →
5. Create labels + milestone → 6. Create issue with labels/milestone →
7. Issue comments, reactions, time tracking, pin/lock →
8. Create PR (branch→main) → 9. Review PR → 10. Merge PR →
11. Create tag → 12. Create release →
13. Wiki pages → 14. Webhooks → 15. Notifications →
16. Actions workflow → 17. Org + teams → 18. Admin operations

---

## CI/CD (build.yml)

Копируем из ticktick-mcp:
- Автоматический version bump (patch)
- `uv build --wheel`
- GitHub Release
- PEP 503 index на GitHub Pages
- Деплой docs/index.html + index

## docs/index.html

Генератор конфига для Claude/MCP клиента:
- Поля ввода: Gitea URL, API Token
- Генерация JSON конфига с env-переменными
- Кнопка копирования

---

## План работ (порядок)

1. Инициализация проекта: `pyproject.toml`, структура пакета, `__init__.py`, `__main__.py`
2. `client.py` — HTTP-клиент для Gitea API
3. `server.py` — все 186 тулов, разбитых по секциям
4. `tests/docker-compose.yml` + `tests/conftest.py` — инфраструктура тестов
5. `tests/test_integration.py` — интеграционные тесты
6. `docs/index.html` — страница генерации конфига
7. `.github/workflows/build.yml` — CI/CD пайплайн
8. README.md
