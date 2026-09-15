# ЗОНА — Android-сборка

## Требования
- Node.js 18+
- Java JDK 17 (`brew install openjdk@17` или скачать с https://adoptium.net)
- Android Studio + Android SDK 34+
- Gradle 8+ (идёт в комплекте)

## Установка зависимостей
```bash
cd /workspace/zona-android
npm install
npx cap sync android
```

## Запуск на устройстве (debug)
```bash
npx cap open android   # Открыть в Android Studio
# Или напрямую:
npx cap run android
```

## Сборка release APK
```bash
# 1. Создай keystore (один раз):
keytool -genkey -v -keystore zona-release.keystore \
  -alias zona -keyalg RSA -keysize 2048 -validity 10000

# 2. Впиши пароль в capacitor.config.json (keystorePassword)

# 3. Собери APK:
cd android
./gradlew assembleRelease
# APK будет в: android/app/build/outputs/apk/release/app-release.apk
```

## Подключение ЮKassa

Замени в `capacitor.config.json`:
```json
"plugins": {
  "Yukassa": {
    "shopId": "ТВОЙ_SHOP_ID",
    "secretKey": "ТВОЙ_SECRET_KEY"
  }
}
```

⚠️ Для production используй serverless-прокси, не вставляй Secret Key напрямую.

## Структура
- `/workspace/zona_prototype/` — игра (HTML/CSS/JS)
- `/workspace/zona-android/` — Capacitor wrapper
  - `capacitor.config.json` — настройки
  - `android/` — Android-проект (после `npx cap add android`)
# Trigger Redeploy Tue Sep 15 18:36:35 UTC 2026

# 1789514081
