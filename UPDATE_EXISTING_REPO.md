# Инструкция: Обновление существующего Git репозитория

## Шаг 1: Узнать URL репозитория

Нужен URL существующего репозитория (HTTPS или SSH). Например:
- `https://github.com/username/repo-name.git`
- `git@github.com:username/repo-name.git`

## Шаг 2: Подключить remote

Если репозиторий на GitHub/GitLab, замените URL на реальный:

```bash
cd /home/artur/Документы/Trade_Bot-master

# Добавить remote (замените YOUR_REPO_URL на реальный URL)
git remote add origin YOUR_REPO_URL

# Проверить что remote добавлен
git remote -v
```

## Шаг 3: Получить историю из удалённого репозитория

**ВАЖНО**: Если в удалённом репозитории уже есть коммиты, нужно сначала их получить:

```bash
# Получить историю из удалённого репозитория
git fetch origin

# Проверить какие ветки есть на удалённом сервере
git branch -r
```

## Шаг 4: Объединить с удалённой историей (если нужно)

Если в удалённом репозитории уже есть коммиты, нужно объединить:

### Вариант A: Если удалённая ветка называется `main`:
```bash
# Переименовать локальную ветку в main (если нужно)
git branch -M main

# Объединить с удалённой веткой main
git pull origin main --allow-unrelated-histories

# Если возникнут конфликты - разрешите их, затем:
git add .
git commit -m "Merge with remote repository"
```

### Вариант B: Если удалённая ветка называется `master`:
```bash
# Объединить с удалённой веткой master
git pull origin master --allow-unrelated-histories

# Если возникнут конфликты - разрешите их, затем:
git add .
git commit -m "Merge with remote repository"
```

## Шаг 5: Выгрузить изменения

После объединения истории (или если удалённый репозиторий пустой):

```bash
# Выгрузить изменения в удалённый репозиторий
git push -u origin master
# или
git push -u origin main  # если ветка называется main
```

## Если репозиторий пустой

Если удалённый репозиторий пустой (нет коммитов), можно сразу сделать push:

```bash
git remote add origin YOUR_REPO_URL
git branch -M main  # или оставить master
git push -u origin main  # или master
```

## Аутентификация

При push вам будет запрошен пароль/токен:

- **GitHub**: Используйте Personal Access Token (не пароль)
  - Settings → Developer settings → Personal access tokens → Generate new token
  - Scope: `repo` (полный доступ)
  
- **GitLab**: Используйте Personal Access Token
  - Preferences → Access Tokens
  - Scopes: `write_repository`

## Пример полной последовательности команд

```bash
cd /home/artur/Документы/Trade_Bot-master

# 1. Добавить remote (замените URL)
git remote add origin https://github.com/username/repo-name.git

# 2. Получить историю
git fetch origin

# 3. Если есть удалённые коммиты - объединить
git pull origin main --allow-unrelated-histories

# 4. Выгрузить
git push -u origin main
```

## Troubleshooting

**Ошибка "remote origin already exists":**
```bash
git remote remove origin
git remote add origin YOUR_REPO_URL
```

**Ошибка "authentication failed":**
- Используйте Personal Access Token вместо пароля
- Проверьте что у вас есть права на push в репозиторий

**Конфликты при merge:**
- Разрешите конфликты в файлах
- `git add .`
- `git commit -m "Resolve merge conflicts"`

