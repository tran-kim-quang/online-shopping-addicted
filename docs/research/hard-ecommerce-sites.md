# Nghiên cứu: vì sao Shopee, Lazada, Amazon… khó crawl

> Ghi chú để nghiên cứu, không phải spec. Các chi tiết kỹ thuật bên dưới (tên header, cookie, endpoint) là quan sát phổ biến tại thời điểm viết. Các sàn đổi cơ chế liên tục, nên **mọi điểm đều cần tự kiểm chứng lại** bằng DevTools trước khi code.

## 0. Bảng tổng quan

| Sàn | Độ khó | Nguyên nhân chính | Hướng khả thi nhất |
|---|---|---|---|
| Tiki | Dễ | API JSON không công bố nhưng mở | Adapter gọi API trực tiếp (baseline) |
| eBay | Trung bình | HTML nặng, bot detection vừa phải | **Browse API chính thức** (free key) |
| Amazon | Khó | Captcha/503, layout không đồng nhất, review cần đăng nhập, không có số bán thật | PA-API (điều kiện khắt khe) hoặc Playwright + chỉ lấy dữ liệu trang SP |
| Shopee | Rất khó | API ký bằng JS obfuscate, yêu cầu đăng nhập, fingerprint thiết bị, captcha | Playwright với profile đã đăng nhập + bắt XHR; hoặc Affiliate API |
| Lazada | Rất khó | Anti-bot của Alibaba (slider captcha), token ký cho API mtop | Playwright + bắt XHR, chấp nhận captcha thủ công |
| TikTok Shop | Rất khó | Ký request (`X-Bogus`, `msToken`…), ưu tiên app hơn web | Để sau cùng |

## 1. Nguyên nhân chung (áp dụng cho mọi sàn lớn)

1. **Render phía client (SPA).** HTML trả về gần như rỗng, dữ liệu nằm trong XHR. Phải dùng trình duyệt thật (Playwright) hoặc tìm ra API.
2. **API nội bộ có chữ ký.** Header hoặc tham số ký được sinh bởi JS đã obfuscate, gắn với timestamp, cookie và fingerprint. Tự sinh chữ ký (reverse engineering) rất tốn công và gãy khi sàn cập nhật.
3. **Fingerprint trình duyệt.** Sàn kiểm tra `navigator.webdriver`, canvas/WebGL, font, TLS fingerprint (JA3/JA4), HTTP/2 fingerprint. Headless Chromium mặc định bị phát hiện dễ dàng.
4. **Uy tín IP.** IP datacenter (VPS/cloud) bị chặn hoặc bị bắt giải captcha ngay. Residential proxy thì tốn phí.
5. **Rate limit và hành vi.** Request quá đều, không cuộn trang, không di chuột đều là dấu hiệu bot.
6. **Bắt đăng nhập.** Ngày càng nhiều dữ liệu (review đầy đủ, trang shop) chỉ hiện khi đã đăng nhập. Crawl bằng tài khoản có thể khiến tài khoản bị khóa.
7. **Dữ liệu "mờ".** Số bán thường làm tròn ("10k+", "Đã bán 1,2k", "500+ bought in past month"), nên xếp hạng "bán chạy nhất" chỉ gần đúng.
8. **Pháp lý.** ToS của hầu hết sàn cấm scraping. Tên và avatar người review là dữ liệu cá nhân (tại VN: Nghị định 13/2023/NĐ-CP). Dự án cá nhân, ít request, không phân phối lại dữ liệu thì rủi ro thấp, nhưng nên đọc kỹ.

## 2. Shopee

**Triệu chứng thường gặp**
- Mở link SP bằng trình duyệt sạch thì bị chuyển sang trang đăng nhập hoặc trang "verify/traffic".
- Gọi trực tiếp các API dạng `/api/v4/pdp/get_pc`, `/api/v4/item/get_ratings`, `/api/v4/shop/...` thì bị 403 hoặc trả dữ liệu rỗng.

**Nguyên nhân**
- Request API kèm các header chống bot (quan sát được dạng `af-ac-enc-dat`, `x-sap-ri`, `x-sap-sec`…) sinh bởi SDK JS obfuscate, gắn với cookie phiên và fingerprint thiết bị.
- Phần lớn dữ liệu yêu cầu phiên đăng nhập. Tài khoản crawl nhiều có thể bị khóa.
- Captcha (slider/puzzle) xuất hiện khi traffic bất thường.
- IP nước ngoài hoặc IP datacenter dễ bị chặn hơn IP dân cư VN.

**Hướng nghiên cứu**
- [ ] Playwright **headful** với `user_data_dir` đã đăng nhập thủ công, vào trang như người thật rồi **bắt response XHR** thay vì tự ký request.
- [ ] Đo xem crawl được bao nhiêu trang hoặc phút trước khi gặp captcha, từ IP nhà và từ VPS.
- [ ] Shopee Affiliate / Open Platform: có API chính thức nào trả về thông tin SP hoặc review không, và điều kiện đăng ký là gì.
- [ ] Phương án "người dùng tự cung cấp": extension trình duyệt gửi DOM của trang đang xem về backend. Cách này né được anti-bot vì chính user đang duyệt.

## 3. Lazada

**Nguyên nhân**
- Dùng hệ anti-bot của Alibaba: trang "punish"/slider captcha, cookie kiểu `x5sec`.
- API mtop yêu cầu token (cookie dạng `_m_h5_tk`) và chữ ký tính từ token, timestamp, appKey và data.
- Dữ liệu SP đôi khi nằm trong biến JS nhúng trong HTML (ví dụ `__moduleData__`), cấu trúc đổi theo từng phiên bản trang.

**Hướng nghiên cứu**
- [ ] Kiểm tra trang SP có JSON nhúng sẵn không: nếu có thì chỉ cần lấy HTML một lần là đủ, không cần gọi API.
- [ ] Bắt XHR review khi cuộn trang bằng Playwright.
- [ ] Đo tần suất gặp slider captcha. Thử phương án mở cửa sổ để user tự giải captcha (human-in-the-loop).
- [ ] Lazada Open Platform / Affiliate: thường dành cho seller hoặc affiliate, xem có endpoint public product nào không.

## 4. Amazon

**Nguyên nhân**
- Bot detection: trang captcha "Enter the characters you see", hoặc lỗi 503 "Sorry, something went wrong", nhất là với IP datacenter.
- **Review đầy đủ cần đăng nhập.** Trang SP chỉ hiện vài review nổi bật cùng tóm tắt do AI tạo ("Customers say…").
- **Không có số bán thật.** Chỉ có nhãn dạng "X+ bought in past month" (làm tròn, không phải SP nào cũng có) và Best Sellers Rank theo danh mục.
- Layout khác nhau theo danh mục và locale (.com, .co.jp, .de…), lại thường xuyên A/B test, nên selector dễ gãy.
- Trang "shop" (storefront hoặc seller page) không có danh sách SP đầy đủ và ổn định như trang shop của Shopee/Tiki.
- Hầu như không có JSON-LD `Product`.

**Hướng nghiên cứu**
- [ ] **Product Advertising API (PA-API)**: cần tài khoản Amazon Associates có doanh số. Kiểm tra trạng thái và phiên bản API hiện tại (Amazon từng thông báo thay đổi API cho affiliate).
- [ ] Playwright từ IP dân cư, chỉ lấy trang SP: title, giá, rating trung bình, phân bố sao, bullet "About this item", review nổi bật, phần "Customers say".
- [ ] Thay "bán chạy nhất shop" bằng **Best Sellers Rank** cùng "bought in past month". Cần ghi rõ cho user đây là số gần đúng.
- [ ] Viết bộ selector có fallback theo từng loại layout, kèm test bằng các trang HTML đã lưu.

## 5. eBay (trung bình, ghi chú để so sánh)

- Có **Browse API chính thức** (đăng ký developer, OAuth client credentials). Lấy được thông tin SP, giá, seller. Đây là đường đi sạch nhất.
- Không có "product review" như các sàn khác: chủ yếu là **seller feedback**, và một số danh mục có catalog review. Câu hỏi kiểu "SP này được phản hồi thế nào" cần định nghĩa lại cho eBay.
- Trang HTML có bot detection nhưng nhẹ hơn Amazon.

## 6. TikTok Shop

- Web ký request bằng các tham số như `X-Bogus`, `msToken`, `_signature`. Chủ yếu dùng qua app, web hạn chế theo vùng.
- Để cuối cùng hoặc bỏ qua.

## 7. Các hướng tiếp cận tổng quát (xếp theo độ "sạch")

1. **API chính thức hoặc affiliate**: ổn định và hợp pháp, nhưng giới hạn dữ liệu và cần đăng ký.
2. **Extension trình duyệt hoặc bookmarklet**: user mở trang, extension gửi DOM/JSON về backend. Không đụng anti-bot vì là phiên thật của user. Rất hợp với use case "dán link rồi hỏi".
3. **Playwright headful, bắt XHR** với profile thật: tốn tài nguyên, chậm, cần IP sạch.
4. **Dịch vụ scraping thương mại** (scraping API hoặc unblocker): trả phí, nhanh có kết quả.
5. **Tự reverse engineering chữ ký**: tốn công nhất, dễ gãy, rủi ro pháp lý cao nhất. Không khuyến nghị.

## 8. Mẫu ghi chép thí nghiệm cho mỗi sàn

```
Sàn:
Ngày thử:
IP: nhà / VPS / proxy (loại)
Cách tiếp cận: httpx / Playwright headless / headful + profile / extension
URL test:
Kết quả: OK / captcha sau N request / 403 / redirect login
Dữ liệu lấy được: title / giá / sold / rating / review (bao nhiêu) / mô tả
Nguồn dữ liệu: JSON-LD / JSON nhúng / XHR (endpoint) / DOM
Ghi chú:
```
