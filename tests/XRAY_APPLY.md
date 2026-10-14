# Применение OpenWrt config через панель роутера (v116)

При отправке textarea браузер кодирует переносы строк как CRLF. CGI
декодировал их без нормализации и записывал CR в исполняемый shell-скрипт.
После появления блока `if/then/else/fi` для сохранения ручного VPS host
это приводило к ошибке `unexpected end of file (expecting "then")`.

CGI удаляет CR перед записью скрипта и проверяет целиком `sh -n` до
выполнения любых команд UCI. При ошибке синтаксиса конфигурация не
меняется. Выполнение через `sh -e` останавливается при ошибке команды,
например при неудачном `render-client`, и не продолжает перезапуск.

`tests/test_xray_apply.py` отправляет настоящий URL-encoded POST в CGI
с тестовым ключом и исполняет команды строгой POSIX-оболочкой. Проверяются
CRLF и LF, heredoc, пустые строки, кодированные символы, отклонение
неполного скрипта до изменений, остановка при сбое команды и отказ
без авторизации. Полный конфиг, сгенерированный Hub, применяется через
ту же форму с подменёнными UCI/службой; ручной VPS host и WAN сохраняются.
На Windows строгая проверка использует Git for Windows dash.

## Установка и откат

Скрипт обновляет только CGI на роутере. VPS обновлять для этого фикса
не требуется. До замены сохраняется первая резервная копия CGI:
`/root/owrt-remote-backup-before-xray-apply-v116/owrt-remote`.

Из PowerShell Windows 11; замените IP, если адрес роутера отличается:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Users\k.zolotarev95\Documents\openwrt overlay\vps\deploy-xray-apply-fix.ps1" -Router "root@192.168.2.1" -Action Install
```

После `XRAY_APPLY_ROUTER_Install_OK` обновите панель роутера через
Ctrl+F5, снова скопируйте полный блок **OpenWrt config** из панели VPS
и нажмите **Применить настройки**. Это повторно применит настройки без
CR и пересоберёт Xray-конфиг. Предыдущая неудачная попытка могла успеть
записать часть настроек; замена CGI сама по себе их не переприменяет.
Если после успешного применения ошибка proxy остаётся, требуется
проверка запуска Xray и обратного соединения с VPS на реальном роутере.

Откат возвращает CGI до установки; настройки, повторно применённые
через форму, остаются текущими:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Users\k.zolotarev95\Documents\openwrt overlay\vps\deploy-xray-apply-fix.ps1" -Router "root@192.168.2.1" -Action Rollback
```

Проверки локально:

```powershell
python tests/test_xray_apply.py -v
```

Переносы строк в формах описаны в [стандарте HTML](https://html.spec.whatwg.org/multipage/form-control-infrastructure.html).

## Мобильная вставка команд (локальный фикс v116)

Воспроизведён отдельный случай `rc=255`: в строке
`uci set owrtremote.main.router_name=‘Дом у моря’` типографские кавычки
не являются кавычками shell. UCI получает лишние аргументы, хотя `sh -n`
не находит ошибки. По одному коду нельзя утверждать, что это причина
конкретного сбоя на телефоне; необходима проверка исходного блока.

В поле команд отключены автокоррекция, автоматический регистр и
автозаполнение. Перед применением удаляется начальный UTF-8 BOM;
в отдельных строках `uci set owrtremote.main...` исправляются неразрывные
разделители и внешние парные типографские кавычки. Содержимое значения
экранируется как буквальная строка: `$`, обратные кавычки и апострофы
не становятся командами. Значения в обычных shell-кавычках, многострочные
строки и heredoc не переписываются. При ошибке вставленный текст остаётся
в поле; настоящие ошибки выполнения по-прежнему останавливают скрипт.

Дополнительные тесты проверяют воспроизведение `255`, исправление мобильной
типографики, сохранение Unicode-значений, отсутствие исполнения символов
из значения, экранирование текста ошибки в HTML и сохранение остановки
при настоящей ошибке UCI.

Обновляется только `/www/cgi-bin/owrt-remote` на роутере, без перезапуска
служб и изменений настроек при установке. Резервная копия отдельная:
`/root/owrt-remote-backup-before-xray-mobile-v116/owrt-remote`.

Установка из PowerShell Windows 11:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Users\k.zolotarev95\Documents\openwrt overlay\vps\deploy-xray-mobile-fix.ps1" -Router "root@192.168.1.1" -Action Install
```

После `XRAY_MOBILE_ROUTER_Install_OK` обновите страницу на телефоне,
снова вставьте полный блок **OpenWrt config** и примените его.

Откат CGI (применённые через форму настройки остаются текущими):

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Users\k.zolotarev95\Documents\openwrt overlay\vps\deploy-xray-mobile-fix.ps1" -Router "root@192.168.1.1" -Action Rollback
```

Проверки установки и отката выполняются на временных файлах без SSH:

```powershell
python tests/test_xray_mobile_deploy.py -v
```
