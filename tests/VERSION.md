# Версия v118

Версия v118 указана в панели VPS, на экране входа, в мобильной шапке,
CGI и представлении LuCI роутера. Скрипты локальной установки, их проверки
и документация используют тот же номер. Ссылки источников не менялись.

Установить локальную сборку на VPS:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Users\k.zolotarev95\Documents\openwrt overlay\vps\deploy-version-v118.ps1" -Vps "root@193.233.82.38" -Action Install
```

В ходе текущего тестирования обновления через кнопку вместо обычной установки
нужно использовать тестовые сборки из [HUB_UPDATE.md](HUB_UPDATE.md):

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Users\k.zolotarev95\Documents\openwrt overlay\vps\test-hub-self-update.ps1" -Vps "root@193.233.82.38" -Action Retest
```

Установить версию интерфейса роутера (CGI и LuCI):

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Users\k.zolotarev95\Documents\openwrt overlay\vps\deploy-version-v118.ps1" -Router "root@192.168.2.1" -Action Install
```

Скрипт сохраняет первую резервную копию и проверяет файлы до установки.
При ошибке после замены возвращает файлы предыдущей попытки.
Установщики не выполняют миграцию настроек роутера; после установки
интерфейса нужно обновить страницу через Ctrl+F5.

Для отката обычной установки VPS либо роутера повторить соответствующую
команду с `-Action Rollback`. Копии:

- VPS: `/opt/owrt-remote/owrt-remote-hub.py.bak-before-v118`.
- Роутер: `/root/owrt-remote-backup-before-v118` (CGI и LuCI).

Для возврата из тестового режима обновления VPS повторить команду
`test-hub-self-update.ps1` с `-Action Restore`: его копия хранится отдельно,
в `/opt/owrt-remote/self-update-local-test/before.py`.
