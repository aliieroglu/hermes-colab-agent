Arka plan araştırmaları tamamlandı ve önceki tavsiyeyi **güçlü biçimde doğruladı**. Ancak birkaç önemli düzeltme ve yeni alternatif ortaya çıktı.

## Kesinleşen mimari karar

**Ana yol değişmiyor:**

> **Notebook CRUD/run için Colab MCP + tarayıcı ve bağlantı işlemleri için Playwright/CDP + ayrı kalıcı Chrome otomasyon profili.**

`computer_use`, UI-TARS veya OmniParser ana yürütücü olmamalı.

## Önemli yeni bulgular

### 1. Koordinat farkı tek bir DPI katsayısı değil

Önceki canlı ölçümde:

- Screenshot: `1404 × 896`
- Native pencere sınırı: `1436 × 938`

Oranlar:

- X: `1.0228`
- Y: `1.0469`

Dolayısıyla sorun basitçe “yaklaşık `1.08` ile çarp” şeklinde çözülemez. Muhtemelen aynı anda şunlar karışıyor:

- decorated outer window,
- client/content area,
- titlebar inset,
- screenshot crop origin,
- Wayland logical coordinate,
- native framebuffer pixel.

`cua-driver` düzeltilecekse tek `scale_factor` yerine bir affine dönüşüm ve şu metadata gerekir:

```json
{
  "outer_bounds": {},
  "client_bounds": {},
  "content_insets": {},
  "crop_origin": {},
  "image_px_size": {},
  "action_space_size": {},
  "pixel_to_action_transform": [],
  "generation": "..."
}
```

Eşleme ispatlanamıyorsa piksel tıklaması **fail-closed** reddedilmeli.

Kaynaklar: [Cua #3061](https://github.com/trycua/cua/issues/3061), [draft PR #3152](https://github.com/trycua/cua/pull/3152)

---

### 2. İncelediğimiz Colab fork’u sandığımızdan daha gelişmiş

`SebastianGilPinzon/colab-mcp` fork’unda zaten:

- araçları başlangıçta önceden kaydetme,
- hücre ekleme/okuma/güncelleme/taşıma/silme,
- çalıştırma ve çıktı okuma,
- stale süreç temizleme,
- IPv4/PNA bağlantı düzeltmeleri,
- `change_runtime`

bulunuyor.

Dolayısıyla daha önce önerdiğim `update_cell` ve `move_cell` araçlarını sıfırdan yazmamıza gerek olmayabilir; mevcut fork’taki uygulamayı sabitleyip test etmeliyiz.

Ancak `change_runtime` dikkat gerektiriyor:

- OAuth ister,
- refresh token’ı dosyada saklar,
- `colaboratory` scope’u kullanır,
- belgelenmemiş `colab.pa.googleapis.com` ve `/tun/m/assign` endpoint’lerine dayanır.

Bu nedenle başlangıçta kullanmamak daha güvenli. Runtime seçimini Playwright ile yapıp sonucu `torch.cuda.get_device_name(0)` ile doğrulamak daha kontrollü.

Kaynak: [SebastianGilPinzon/colab-mcp](https://github.com/SebastianGilPinzon/colab-mcp)

---

### 3. ByteDance tarafında kullanılabilir gerçek bir Browser MCP var

Agent TARS’ın tamamı Hermes’e bağlanabilecek hazır bir MCP sunucusu değil; esasen MCP araçlarını tüketen ikinci bir ajan.

Fakat monorepo içinde bağımsız bir paket var:

```text
@agent-infra/mcp-server-browser
```

Özellikleri:

- Puppeteer tabanlı,
- DOM ve accessibility etiketleriyle çalışıyor,
- stdio, SSE ve Streamable HTTP destekliyor,
- normal DOM modunda vision modeli gerektirmiyor,
- görsel mod opsiyonel.

Bu paketi Hermes’e doğrudan MCP olarak bağlamak teknik olarak mümkün. [Browser MCP kaynak dizini](https://github.com/bytedance/UI-TARS-desktop/tree/main/packages/agent-infra/mcp-servers/browser)

Fakat Colab için yine de **Playwright’ı tercih ederim**, çünkü:

- kodumuza doğrudan gömülebilir,
- connection token ve notebook sekmesiyle aynı servis içinde çalışabilir,
- locator, trace ve test altyapısı daha güçlü,
- ekstra bir MCP/runtime katmanı oluşturmaz.

ByteDance Browser MCP’yi sıfırdan yazacağımız browser sidecar’a karşı kısa bir A/B pilotta test etmek mantıklı olabilir.

---

### 4. UI-TARS benchmark rakamları dikkatli okunmalı

`47.5 OSWorld` iddiası UI-TARS-2 teknik raporuna dayanıyor. Ancak UI-TARS-2:

- yaklaşık `230B` toplam,
- `23B` aktif parametreli MoE,

ve genel kullanıma açık 7B modelle aynı şey değil.

Açık `UI-TARS-1.5-7B` için sonuçlar yaklaşık:

- self-reported: `26.92–27.5`
- doğrulanmış koşullara göre: `25.2–29.6`

civarında.

Ayrıca resmi deployment rehberi 7B model için bile L40S/A100 sınıfı, yaklaşık 48 GB GPU belleği öneriyor. Yalnız Colab butonlarına daha iyi tıklamak için böyle bir modeli çalıştırmak gereksiz pahalı.

---

### 5. OmniParser yararlı ama coordinate problemini çözmez

OmniParser:

- küçük ikonları,
- metinleri,
- etkileşimli bölgeleri,
- bounding box’ları

çok daha iyi bulabilir. Fakat sonuçları hâlâ screenshot pixel uzayındadır.

Yani şu dönüşüm yine zorunlu:

```text
OmniParser bounding box
  → screenshot pixel
  → crop/padding düzeltmesi
  → CSS/logical coordinate
  → native input coordinate
```

Dolayısıyla coordinate mapping bozuksa doğru butonu bulup yanlış yere tıklayabilir.

Ek bulgular:

- Resmî MCP sunucusu yok; MCP talebi hâlâ açık.
- Repo lisansında çelişki var: kök lisans CC-BY-4.0, bazı yeni ağırlıklar MIT, eski Ultralytics detector’lar AGPL.
- Ana sisteme gömmeden önce belirli kod ve ağırlık sürümünü lisans açısından sabitlemek gerekir.

OmniParser yalnız şu fallback sırasında kullanılmalı:

```text
typed-browser DOM ref
→ native AX element
→ OmniParser visual element
→ doğrulanmış coordinate click
```

---

## Revize edilmiş uygulama planı

### Faz 1 — En düşük riskli bağlantı katmanı

- Ayrı persistent Chrome automation profile.
- Kullanıcının bir kez Google login/2FA yapması.
- Playwright `launchPersistentContext`.
- CDP yalnız `127.0.0.1`.
- Notebook URL/Drive ID ile sekme seçimi.
- MCP token ve port fragment’inin doğrudan sekmeye eklenmesi.
- `get_cells` ile otomatik bağlantı doğrulaması.

### Faz 2 — Mevcut fork’u ürünleştirme

Yeni araç yazmadan önce `SebastianGilPinzon/colab-mcp` içindeki mevcut araçları test edip sabitlemek:

```text
get_cells
add_code_cell
add_text_cell
update_cell
move_cell
delete_cell
run_code_cell
```

Ardından eksik olanları eklemek:

```text
open_notebook
select_notebook
get_runtime_status
interrupt_runtime
restart_runtime
wait_for_cell
upload_file
download_file
reconnect_notebook
```

### Faz 3 — QTIP iş dayanıklılığı

- aşamalı job manifesti,
- checksum’lı checkpointler,
- Drive/Hugging Face’e kalıcı kayıt,
- heartbeat/log dosyası,
- tarayıcı kopunca yeniden bağlanma,
- idempotent stage çalıştırma.

### Faz 4 — Genel Computer Use geliştirmesi

Colab tamamlandıktan sonra:

- Cua PR `#3152` merge durumunu takip etme,
- gerekirse coordinate-normalization kısmını backport etme,
- outer/client bounds ve inset metadata ekleme,
- OmniParser’ı yalnız optional visual fallback yapma.

## Sonuç

En verimli yol:

```text
Hermes
  └─ Enhanced Colab MCP
       ├─ Colab notebook araçları
       ├─ Runtime/job/file araçları
       └─ Playwright persistent browser manager
```

ByteDance Browser MCP küçük bir karşılaştırma pilotu olabilir. Agent TARS’ın tamamı, UI-TARS modeli ve SOC bu proje için gereksiz karmaşıklık getiriyor.
