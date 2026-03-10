/*
 * ═══════════════════════════════════════════════════════════
 *  TROPHY RAID — LoRa Mesh Tracker / Gateway v2
 *  Каждый узел хранит таблицу ВСЕХ узлов и передаёт её целиком
 * ═══════════════════════════════════════════════════════════
 *
 *  РЕЖИМ:
 */

#define MODE_TRACKER    // Трекер: GPS + LoRa (Wi-Fi выключен)
//#define MODE_GATEWAY // База: LoRa + Wi-Fi API (GPS выключен)

#define NODE_ID  2      // Уникальный ID (1..254, 0 = невалидный)

/*
 * ═══════════════════════════════════════════════════════════
 *  ПРОТОКОЛ v2 — «снимок мира»
 * ═══════════════════════════════════════════════════════════
 *
 *  Каждый узел хранит таблицу всех известных узлов.
 *  При отправке шлёт:
 *    [Header 3B] + [Entry × N] (N = кол-во известных узлов)
 *
 *  Header (3 байта):
 *    [SenderID:1] [SeqNum:1] [EntryCount:1]
 *
 *  Entry (20 байт):
 *    [NodeID:1] [Lat:4] [Lon:4] [Alt:2] [Speed:1]
 *    [Heading:1] [Battery:1] [HDOP:1] [Timestamp:4] [Flags:1]
 *
 *  Максимум: 3 + 30×20 = 603 байт
 *  E220 буфер 512 байт → макс ~25 записей за раз
 *  При >25 узлов — разбиваем на несколько пакетов
 *
 *  Логика обновления:
 *    Приняли Entry с NodeID=X, Timestamp=T
 *    Если T > нашего Timestamp для X → обновляем
 *    Если T <= нашего → игнорируем (у нас свежее)
 *
 * ═══════════════════════════════════════════════════════════
 *
 *  ПОДКЛЮЧЕНИЕ:
 *    ATGM336H (трекер): TX→GPIO17, RX→GPIO16, PPS→GPIO12
 *    E220-900T22D: TXD→GPIO4, RXD→GPIO2, M0→GPIO32, M1→GPIO33, AUX→GPIO35
 *    Питание: 3.3V, конденсаторы 100µF+100nF на E220 VCC
 *
 *  Библиотеки: TinyGPSPlus
 *  Board: ESP32 Dev Module
 */

#include <Arduino.h>

#ifdef MODE_TRACKER
  #include <TinyGPSPlus.h>
  #include <WiFi.h>
#endif
#ifdef MODE_GATEWAY
  #include <WiFi.h>
  #include <WebServer.h>
#endif

// ==================== ПИНЫ ====================
#define GPS_RX    16
#define GPS_TX    17
#define GPS_PPS   4
#define GPS_BAUD  9600

#define E220_RX   14
#define E220_TX   27
#define E220_M0   32
#define E220_M1   33
#define E220_AUX  15
#define E220_BAUD 9600

#define LED_PIN   2

// ==================== НАСТРОЙКИ ====================
#define MAX_NODES       35    // макс. узлов в таблице
#define ENTRY_SIZE      20    // байт на запись
#define HEADER_SIZE     3     // байт заголовок
#define E220_MAX_PKT    512   // буфер E220
#define MAX_ENTRIES_PKT ((E220_MAX_PKT - HEADER_SIZE) / ENTRY_SIZE)  // 25

#define TX_INTERVAL     30000
#define TX_JITTER       5000
#define RELAY_JITTER_MIN 200
#define RELAY_JITTER_MAX 800

// Gateway Wi-Fi
#ifdef MODE_GATEWAY
  #define WIFI_AP_SSID "TrophyGW"
  #define WIFI_AP_PASS "12345678"
  #define WIFI_STA_SSID "DIR-615-eb25"
  #define WIFI_STA_PASS "23857643"
#endif

// ==================== ТАБЛИЦА УЗЛОВ ====================

struct NodeEntry {
  uint8_t  id;          // 0 = пустой слот
  int32_t  lat;         // × 10^7
  int32_t  lon;         // × 10^7
  int16_t  alt;         // метры
  uint8_t  speed;       // км/ч
  uint8_t  heading;     // 0-255 → 0°-360°
  uint8_t  battery;     // %
  uint8_t  hdop;        // × 10
  uint32_t timestamp;   // UTC секунды от полуночи (из GPS)
  uint8_t  flags;       // bit0=SOS
  // Локальные поля (не передаются):
  unsigned long localRxTime;  // millis() когда получили
};

NodeEntry nodeTable[MAX_NODES];
uint8_t mySeqNum = 0;

// Дедупликация (sender + seq)
#define DEDUP_SIZE 60
struct DedupeEntry { uint8_t sender; uint8_t seq; };
DedupeEntry dedupBuf[DEDUP_SIZE];
int dedupPos = 0;

// Статистика
unsigned long statRx = 0, statTx = 0, statRelay = 0, statUpdated = 0;

// ==================== ОБЪЕКТЫ ====================
HardwareSerial loraSerial(2);

#ifdef MODE_TRACKER
  HardwareSerial gpsSerial(1);
  TinyGPSPlus gps;
  volatile unsigned long ppsCount = 0;
  volatile unsigned long ppsInterval = 0;
  static unsigned long ppsPrev = 0;
  unsigned long nextTxTime = 0;
#endif

#ifdef MODE_GATEWAY
  WebServer server(80);
#endif

// ==================== ТАБЛИЦА: ОПЕРАЦИИ ====================

NodeEntry* findNode(uint8_t id) {
  for (int i = 0; i < MAX_NODES; i++) {
    if (nodeTable[i].id == id) return &nodeTable[i];
  }
  return nullptr;
}

NodeEntry* allocNode(uint8_t id) {
  // Сначала ищем существующий
  NodeEntry* n = findNode(id);
  if (n) return n;
  // Ищем пустой слот
  for (int i = 0; i < MAX_NODES; i++) {
    if (nodeTable[i].id == 0) {
      nodeTable[i].id = id;
      return &nodeTable[i];
    }
  }
  // Таблица полная — вытесняем самый старый
  unsigned long oldest = ULONG_MAX;
  int oldestIdx = 0;
  for (int i = 0; i < MAX_NODES; i++) {
    if (nodeTable[i].localRxTime < oldest) {
      oldest = nodeTable[i].localRxTime;
      oldestIdx = i;
    }
  }
  nodeTable[oldestIdx].id = id;
  return &nodeTable[oldestIdx];
}

int countNodes() {
  int c = 0;
  for (int i = 0; i < MAX_NODES; i++) {
    if (nodeTable[i].id != 0) c++;
  }
  return c;
}

// ==================== E220 ====================

bool waitAux(unsigned long timeout = 1000) {
  unsigned long start = millis();
  while (digitalRead(E220_AUX) == LOW) {
    if (millis() - start > timeout) return false;
    delay(1);
  }
  return true;
}

void initLora() {
  pinMode(E220_M0, OUTPUT);
  pinMode(E220_M1, OUTPUT);
  pinMode(E220_AUX, INPUT);
  digitalWrite(E220_M0, LOW);
  digitalWrite(E220_M1, LOW);
  delay(50);
  loraSerial.begin(E220_BAUD, SERIAL_8N1, E220_RX, E220_TX);
  delay(200);
  if (waitAux(2000)) Serial.println("[LORA] E220 готов");
  else Serial.println("[LORA] E220 не отвечает!");
}

// ==================== ДЕДУПЛИКАЦИЯ ====================

bool isDuplicate(uint8_t sender, uint8_t seq) {
  for (int i = 0; i < DEDUP_SIZE; i++) {
    if (dedupBuf[i].sender == sender && dedupBuf[i].seq == seq) return true;
  }
  return false;
}

void addDedup(uint8_t sender, uint8_t seq) {
  dedupBuf[dedupPos] = {sender, seq};
  dedupPos = (dedupPos + 1) % DEDUP_SIZE;
}

// ==================== СЕРИАЛИЗАЦИЯ ====================

void serializeEntry(const NodeEntry& e, uint8_t* buf) {
  buf[0] = e.id;
  buf[1] = (e.lat >> 24); buf[2] = (e.lat >> 16); buf[3] = (e.lat >> 8); buf[4] = e.lat;
  buf[5] = (e.lon >> 24); buf[6] = (e.lon >> 16); buf[7] = (e.lon >> 8); buf[8] = e.lon;
  buf[9] = (e.alt >> 8); buf[10] = e.alt;
  buf[11] = e.speed;
  buf[12] = e.heading;
  buf[13] = e.battery;
  buf[14] = e.hdop;
  buf[15] = (e.timestamp >> 24); buf[16] = (e.timestamp >> 16);
  buf[17] = (e.timestamp >> 8); buf[18] = e.timestamp;
  buf[19] = e.flags;
}

void deserializeEntry(const uint8_t* buf, NodeEntry& e) {
  e.id = buf[0];
  e.lat = ((int32_t)buf[1]<<24)|((int32_t)buf[2]<<16)|((int32_t)buf[3]<<8)|buf[4];
  e.lon = ((int32_t)buf[5]<<24)|((int32_t)buf[6]<<16)|((int32_t)buf[7]<<8)|buf[8];
  e.alt = ((int16_t)buf[9]<<8)|buf[10];
  e.speed = buf[11];
  e.heading = buf[12];
  e.battery = buf[13];
  e.hdop = buf[14];
  e.timestamp = ((uint32_t)buf[15]<<24)|((uint32_t)buf[16]<<16)|((uint32_t)buf[17]<<8)|buf[18];
  e.flags = buf[19];
}

// ==================== ОТПРАВКА ТАБЛИЦЫ ====================

bool isTimestampNewer(uint32_t incomingTs, uint32_t currentTs) {
  if (incomingTs == currentTs) return false;
  // timestamp = секунды в пределах суток, учитываем переход через 00:00
  const uint32_t DAY_SECONDS = 86400UL;
  uint32_t diff = (incomingTs + DAY_SECONDS - currentTs) % DAY_SECONDS;
  return diff > 0 && diff < (DAY_SECONDS / 2);
}

void broadcastTable() {
  if (!waitAux(500)) {
    Serial.println("[TX] AUX занят");
    return;
  }
  
  // Собираем валидные записи
  int count = 0;
  for (int i = 0; i < MAX_NODES; i++) {
    if (nodeTable[i].id != 0) count++;
  }
  if (count == 0) return;
  
  // Разбиваем на пакеты по MAX_ENTRIES_PKT записей
  int sent = 0;
  int entryIdx = 0;
  
  while (sent < count) {
    int batchSize = min(count - sent, (int)MAX_ENTRIES_PKT);
    int pktLen = HEADER_SIZE + batchSize * ENTRY_SIZE;
    uint8_t pkt[E220_MAX_PKT];
    
    // Заголовок
    uint8_t pktSeq = mySeqNum++;
    pkt[0] = NODE_ID;
    pkt[1] = pktSeq;
    pkt[2] = batchSize;
    
    // Записи
    int pos = HEADER_SIZE;
    int added = 0;
    for (int i = entryIdx; i < MAX_NODES && added < batchSize; i++) {
      if (nodeTable[i].id == 0) continue;
      serializeEntry(nodeTable[i], pkt + pos);
      pos += ENTRY_SIZE;
      added++;
      entryIdx = i + 1;
    }
    
    // Отправка
    if (waitAux(300)) {
      loraSerial.write(pkt, pktLen);
      statTx++;
      Serial.printf("[TX] seq:%d записей:%d размер:%dB\n", pktSeq, batchSize, pktLen);
      addDedup(NODE_ID, pktSeq);
    }
    
    sent += batchSize;
    
    // Пауза между частями если несколько пакетов
    if (sent < count) {
      delay(random(100, 300));
    }
  }
  
}

// ==================== ПРИЁМ ====================

void processReceived(uint8_t* buf, int len) {
  if (len < HEADER_SIZE + ENTRY_SIZE) return;  // минимум заголовок + 1 запись
  
  uint8_t sender = buf[0];
  uint8_t seq = buf[1];
  uint8_t entryCount = buf[2];
  
  // Проверки
  if (sender == NODE_ID) return;   // свой пакет (эхо)
  if (entryCount == 0) return;
  if (len < HEADER_SIZE + entryCount * ENTRY_SIZE) return;  // битый пакет
  
  // Дедупликация
  if (isDuplicate(sender, seq)) return;
  addDedup(sender, seq);
  statRx++;
  
  Serial.printf("[RX] от:%d seq:%d записей:%d\n", sender, seq, entryCount);
  
  // Обработка каждой записи
  int updated = 0;
  for (int i = 0; i < entryCount; i++) {
    NodeEntry incoming;
    deserializeEntry(buf + HEADER_SIZE + i * ENTRY_SIZE, incoming);
    
    if (incoming.id == 0) continue;
    if (incoming.id == NODE_ID) continue;  // свои данные не перезаписываем чужими
    
    NodeEntry* existing = findNode(incoming.id);
    
    if (existing) {
      // Обновляем только если входящий timestamp новее
      if (isTimestampNewer(incoming.timestamp, existing->timestamp)) {
        existing->lat = incoming.lat;
        existing->lon = incoming.lon;
        existing->alt = incoming.alt;
        existing->speed = incoming.speed;
        existing->heading = incoming.heading;
        existing->battery = incoming.battery;
        existing->hdop = incoming.hdop;
        existing->timestamp = incoming.timestamp;
        existing->flags = incoming.flags;
        existing->localRxTime = millis();
        updated++;
      }
    } else {
      // Новый узел
      NodeEntry* slot = allocNode(incoming.id);
      slot->lat = incoming.lat;
      slot->lon = incoming.lon;
      slot->alt = incoming.alt;
      slot->speed = incoming.speed;
      slot->heading = incoming.heading;
      slot->battery = incoming.battery;
      slot->hdop = incoming.hdop;
      slot->timestamp = incoming.timestamp;
      slot->flags = incoming.flags;
      slot->localRxTime = millis();
      updated++;
    }
  }
  
  statUpdated += updated;
  if (updated) Serial.printf("  → обновлено: %d узлов\n", updated);
  
  // Ретрансляция: пересылаем как есть (данные не наши — передаём дальше)
  delay(random(RELAY_JITTER_MIN, RELAY_JITTER_MAX));
  if (waitAux(300)) {
    loraSerial.write(buf, len);
    statRelay++;
    Serial.printf("[RELAY] от:%d seq:%d\n", sender, seq);
  }
  
  #if LED_PIN >= 0
    digitalWrite(LED_PIN, HIGH); delay(20); digitalWrite(LED_PIN, LOW);
  #endif
}

// ==================== LORA ПРИЁМ (буферизация) ====================

uint8_t loraBuf[E220_MAX_PKT + 16];
int loraBufPos = 0;
unsigned long lastLoraByte = 0;

void loraReceiveLoop() {
  while (loraSerial.available()) {
    if (loraBufPos < (int)sizeof(loraBuf)) {
      loraBuf[loraBufPos++] = loraSerial.read();
    } else {
      loraSerial.read();
    }
    lastLoraByte = millis();
  }
  
  // Конец пакета: 30мс тишины (увеличено из-за больших пакетов)
  if (loraBufPos > 0 && millis() - lastLoraByte > 30) {
    processReceived(loraBuf, loraBufPos);
    loraBufPos = 0;
  }
}

// ==================== ТРЕКЕР ====================

#ifdef MODE_TRACKER

void IRAM_ATTR ppsISR() {
  unsigned long now = millis();
  if (ppsPrev > 0) ppsInterval = now - ppsPrev;
  ppsPrev = now;
  ppsCount++;
}

uint8_t readBattery() {
  // TODO: ADC с делителя
  return 100;
}

uint32_t gpsTimeToSeconds() {
  if (!gps.time.isValid()) return 0;
  return gps.time.hour() * 3600UL + gps.time.minute() * 60UL + gps.time.second();
}

void updateMyPosition() {
  if (!gps.location.isValid()) return;
  
  NodeEntry* me = allocNode(NODE_ID);
  me->lat = (int32_t)(gps.location.lat() * 1e7);
  me->lon = (int32_t)(gps.location.lng() * 1e7);
  me->alt = (int16_t)gps.altitude.meters();
  me->speed = min((int)gps.speed.kmph(), 255);
  me->heading = (uint8_t)(gps.course.deg() * 255.0 / 360.0);
  me->battery = readBattery();
  me->hdop = min((int)(gps.hdop.hdop() * 10), 255);
  me->timestamp = gpsTimeToSeconds();
  me->flags = 0;  // TODO: SOS кнопка
  me->localRxTime = millis();
}

void trackerLoop() {
  while (gpsSerial.available()) {
    gps.encode(gpsSerial.read());
  }
  
  // Обновляем свою запись постоянно (но отправляем по таймеру)
  updateMyPosition();
  
  // Отправка всей таблицы
  if (millis() >= nextTxTime) {
    broadcastTable();
    nextTxTime = millis() + TX_INTERVAL + random(-TX_JITTER, TX_JITTER);
  }
}

void trackerDebug() {
  NodeEntry* me = findNode(NODE_ID);
  Serial.printf("[INFO] GPS:%s sats:%d | Таблица:%d узлов | tx:%lu rx:%lu relay:%lu upd:%lu | PPS:%lu | up:%lus\n",
    gps.location.isValid() ? "FIX" : "---",
    gps.satellites.value(),
    countNodes(),
    statTx, statRx, statRelay, statUpdated,
    ppsCount,
    millis() / 1000);
  
  if (me && me->lat != 0) {
    Serial.printf("  Я: %.6f, %.6f %dm %dkm/h ts:%lu\n",
      me->lat / 1e7, me->lon / 1e7, me->alt, me->speed, me->timestamp);
  }
  
  // Вывод всей таблицы
  for (int i = 0; i < MAX_NODES; i++) {
    if (nodeTable[i].id == 0 || nodeTable[i].id == NODE_ID) continue;
    unsigned long age = (millis() - nodeTable[i].localRxTime) / 1000;
    Serial.printf("  #%d: %.6f, %.6f %dm %dkm/h ts:%lu age:%lus%s\n",
      nodeTable[i].id,
      nodeTable[i].lat / 1e7, nodeTable[i].lon / 1e7,
      nodeTable[i].alt, nodeTable[i].speed,
      nodeTable[i].timestamp, age,
      (nodeTable[i].flags & 1) ? " SOS!" : "");
  }
  
  if (gps.charsProcessed() < 10) {
    Serial.println("[WARN] GPS не отвечает!");
  }
}

#endif // MODE_TRACKER

// ==================== GATEWAY ====================

#ifdef MODE_GATEWAY

void handleApi() {
  unsigned long now = millis();
  
  String json = "{";
  json += "\"node_id\":" + String(NODE_ID);
  json += ",\"uptime\":" + String(now / 1000);
  json += ",\"free_heap\":" + String(ESP.getFreeHeap());
  json += ",\"stats\":{\"rx\":" + String(statRx);
  json += ",\"relay\":" + String(statRelay);
  json += ",\"updated\":" + String(statUpdated) + "}";
  
  json += ",\"nodes\":[";
  bool first = true;
  for (int i = 0; i < MAX_NODES; i++) {
    if (nodeTable[i].id == 0) continue;
    unsigned long age = now - nodeTable[i].localRxTime;
    if (age > 600000) continue;  // >10 мин — пропускаем
    
    if (!first) json += ",";
    json += "{\"id\":" + String(nodeTable[i].id);
    json += ",\"lat\":" + String(nodeTable[i].lat / 1e7, 7);
    json += ",\"lon\":" + String(nodeTable[i].lon / 1e7, 7);
    json += ",\"alt\":" + String(nodeTable[i].alt);
    json += ",\"speed\":" + String(nodeTable[i].speed);
    json += ",\"heading\":" + String((int)(nodeTable[i].heading * 360.0 / 255.0));
    json += ",\"battery\":" + String(nodeTable[i].battery);
    json += ",\"hdop\":" + String(nodeTable[i].hdop / 10.0, 1);
    json += ",\"timestamp\":" + String(nodeTable[i].timestamp);
    json += ",\"flags\":" + String(nodeTable[i].flags);
    json += ",\"age\":" + String(age) + "}";
    first = false;
  }
  json += "]}";
  
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.send(200, "application/json", json);
}

void handle404() {
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.send(404, "application/json", "{\"error\":\"not found\"}");
}

void initWiFi() {
  #ifdef WIFI_STA_SSID
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_STA_SSID, WIFI_STA_PASS);
    Serial.print("[WIFI] Подключение");
    int att = 0;
    while (WiFi.status() != WL_CONNECTED && att < 40) { delay(500); Serial.print("."); att++; }
    if (WiFi.status() == WL_CONNECTED) {
      Serial.printf("\n[WIFI] IP: %s\n", WiFi.localIP().toString().c_str());
    } else {
      Serial.println("\n[WIFI] Не удалось, запуск AP");
      WiFi.mode(WIFI_AP);
      WiFi.softAP(WIFI_AP_SSID, WIFI_AP_PASS);
      delay(200);
      Serial.printf("[WIFI] AP: %s IP: %s\n", WIFI_AP_SSID, WiFi.softAPIP().toString().c_str());
    }
  #else
    WiFi.mode(WIFI_AP);
    WiFi.softAP(WIFI_AP_SSID, WIFI_AP_PASS);
    delay(200);
    Serial.printf("[WIFI] AP: %s Пароль: %s\n", WIFI_AP_SSID, WIFI_AP_PASS);
    Serial.printf("[WIFI] IP: %s\n", WiFi.softAPIP().toString().c_str());
  #endif
  
  server.on("/", handleApi);
  server.onNotFound(handle404);
  server.begin();
}

void gatewayLoop() {
  server.handleClient();
}

void gatewayDebug() {
  Serial.printf("[INFO] Узлов: %d | rx:%lu relay:%lu upd:%lu | Heap:%lu | up:%lus\n",
    countNodes(), statRx, statRelay, statUpdated,
    ESP.getFreeHeap(), millis() / 1000);
  
  for (int i = 0; i < MAX_NODES; i++) {
    if (nodeTable[i].id == 0) continue;
    unsigned long age = (millis() - nodeTable[i].localRxTime) / 1000;
    Serial.printf("  #%d: %.6f, %.6f %dm %dkm/h bat:%d%% ts:%lu age:%lus%s\n",
      nodeTable[i].id,
      nodeTable[i].lat / 1e7, nodeTable[i].lon / 1e7,
      nodeTable[i].alt, nodeTable[i].speed, nodeTable[i].battery,
      nodeTable[i].timestamp, age,
      (nodeTable[i].flags & 1) ? " SOS!" : "");
  }
}

#endif // MODE_GATEWAY

// ==================== SETUP ====================

void setup() {
  Serial.begin(115200);
  delay(500);
  
  Serial.println();
  Serial.println("════════════════════════════════════════");
  #ifdef MODE_TRACKER
    Serial.printf("  TROPHY RAID v2 — ТРЕКЕР #%d\n", NODE_ID);
    Serial.println("  Протокол: снимок таблицы всех узлов");
  #else
    Serial.printf("  TROPHY RAID v2 — БАЗА #%d\n", NODE_ID);
    Serial.println("  Протокол: снимок таблицы всех узлов");
  #endif
  Serial.println("════════════════════════════════════════");
  Serial.println();
  
  #if LED_PIN >= 0
    pinMode(LED_PIN, OUTPUT);
  #endif
  
  memset(nodeTable, 0, sizeof(nodeTable));
  memset(dedupBuf, 0, sizeof(dedupBuf));
  
  initLora();
  
  #ifdef MODE_TRACKER
    gpsSerial.begin(GPS_BAUD, SERIAL_8N1, GPS_RX, GPS_TX);
    Serial.printf("[GPS] UART1 RX=%d TX=%d\n", GPS_RX, GPS_TX);
    pinMode(GPS_PPS, INPUT_PULLDOWN);
    attachInterrupt(digitalPinToInterrupt(GPS_PPS), ppsISR, RISING);
    WiFi.mode(WIFI_OFF);
    btStop();
    Serial.println("[WIFI] Выключен");
    nextTxTime = millis() + 10000;
    Serial.println("\n[OK] Трекер запущен. Ожидание GPS...\n");
  #endif
  
  #ifdef MODE_GATEWAY
    initWiFi();
    Serial.println("\n[OK] База запущена. Ожидание пакетов...\n");
  #endif
}

// ==================== LOOP ====================

void loop() {
  loraReceiveLoop();
  
  #ifdef MODE_TRACKER
    trackerLoop();
  #endif
  #ifdef MODE_GATEWAY
    gatewayLoop();
  #endif
  
  static unsigned long lastDbg = 0;
  if (millis() - lastDbg >= 10000) {
    lastDbg = millis();
    #ifdef MODE_TRACKER
      trackerDebug();
    #endif
    #ifdef MODE_GATEWAY
      gatewayDebug();
    #endif
  }
}
