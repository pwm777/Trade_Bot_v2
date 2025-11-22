# Инструкция: Выгрузка проекта в Git

## Шаг 1: Создать репозиторий на GitHub/GitLab

1. **GitHub**: https://github.com/new
   - Введите имя репозитория (например: `Trade_Bot`)
   - Выберите Public/Private
   - **НЕ** ставьте галочки "Add README", "Add .gitignore", "Add license"
   - Нажмите "Create repository"

2. **GitLab**: https://gitlab.com/projects/new
   - Введите имя проекта
   - **НЕ** инициализируйте репозиторий с файлами
   - Нажмите "Create project"

## Шаг 2: Подключить remote и сделать push

После создания репозитория скопируйте URL (HTTPS или SSH).

### Вариант A: HTTPS (проще, требуется пароль/токен)
```bash
cd /home/artur/Документы/Trade_Bot-master

# Подключить remote (замените YOUR_USERNAME и YOUR_REPO_NAME)
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO_NAME.git

# Переименовать ветку в main (если нужно)
git branch -M main

# Выгрузить код
git push -u origin main
```

### Вариант B: SSH (нужен настроенный SSH ключ)
```bash
cd /home/artur/Документы/Trade_Bot-master

# Подключить remote (замените YOUR_USERNAME и YOUR_REPO_NAME)
git remote add origin git@github.com:YOUR_USERNAME/YOUR_REPO_NAME.git

# Переименовать ветку в main (если нужно)
git branch -M main

# Выгрузить код
git push -u origin main
```

## Шаг 3: Проверка

После успешного push:
```bash
git remote -v  # Показать подключенные remotes
git status     # Проверить статус
```

## Примеры команд

### Для GitHub (HTTPS):
```bash
git remote add origin https://github.com/artur/Trade_Bot.git
git branch -M main
git push -u origin main
```

### Для GitLab (HTTPS):
```bash
git remote add origin https://gitlab.com/artur/Trade_Bot.git
git branch -M main
git push -u origin main
```

## Если репозиторий на GitHub уже существует

Если вы клонировали проект, remote может быть уже настроен:
```bash
git remote -v  # Проверить существующие remotes
git branch -M main  # Переименовать ветку
git push -u origin main  # Выгрузить
```

## Troubleshooting

**Ошибка "repository not found":**
- Проверьте правильность URL
- Убедитесь что репозиторий создан и доступен

**Ошибка "authentication failed":**
- Для HTTPS: используйте Personal Access Token вместо пароля
  - GitHub: Settings → Developer settings → Personal access tokens
  - GitLab: Preferences → Access Tokens
- Для SSH: убедитесь что SSH ключ добавлен в GitHub/GitLab

**Ошибка "remote origin already exists":**
- Удалить старый: `git remote remove origin`
- Добавить новый: `git remote add origin YOUR_URL`

