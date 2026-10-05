# HƯỚNG DẪN SỬ DỤNG VÀ TÀI LIỆU KỸ THUẬT HỆ THỐNG
## HỆ THỐNG PHÂN TÍCH CHẤT LƯỢNG BẢO TRÌ SÀI GÒN 01 (TOOL MYBAE AUTO)
**Tác giả:** phongnh5 | **Đơn vị:** FPT Telecom - Chi nhánh Sài Gòn 01

---

## MỤC LỤC
1. [Giới Thiệu Tổng Quan](#1-giới-thiệu-tổng-quan)
2. [Kiến Trúc Kỹ Thuật & Tích Hợp Hệ Thống](#2-kiến-trúc-kỹ-thuật--tích-hợp-hệ-thống)
3. [Chi Tiết Cách Tính & Quy Tắc Đánh Giá Các Chỉ Số Phân Tích Chuyên Sâu](#3-chi-tiết-cách-tính--quy-tắc-đánh-giá-các-chỉ-số-phân-tích-chuyên-sâu)
   - [3.1. Chẩn đoán hệ thống & Xử lý lỗi tự động](#31-chẩn-đoán-hệ-thống--xử-lý-lỗi-tự-động)
   - [3.2. Cảnh báo, Cần xử lý & Quy tắc đổi Modem WF6](#32-cảnh-báo-cần-xử-lý--quy-tắc-đổi-modem-wf6)
   - [3.3. Đánh giá Công suất thu Rx Power (Suy hao quang)](#33-đánh-giá-công-suất-thu-rx-power-suy-hao-quang)
   - [3.4. Đối chiếu Rớt Kết Nối (Disconnections Cross-Check)](#34-đối-chiếu-rớt-kết-nối-disconnections-cross-check)
   - [3.5. Đối chiếu Hợp đồng cùng Tập Điểm (Tap-Point Cross-Check)](#35-đối-chiếu-hợp-đồng-cùng-tập-điểm-tap-point-cross-check)
   - [3.6. Phân tích Thiết bị Kết Nối Kém (Client Analysis)](#36-phân-tích-thiết-bị-kết-nối-kém-client-analysis)
   - [3.7. Chuẩn hóa & Cô đọng nội dung Auto Note lên FPT Inside](#37-chuẩn-hóa--cô-đọng-nội-dung-auto-note-lên-fpt-inside)
4. [Các Chức Năng Chính Trên Giao Diện Web](#4-các-chức-năng-chính-trên-giao-diện-web)
5. [Hướng Dẫn Cài Đặt & Vận Hành Hệ Thống](#5-hướng-dẫn-cài-đặt--vận-hành-hệ-thống)
6. [Xử Lý Sự Cố Thường Gặp (Troubleshooting)](#6-xử-lý-sự-cố-thường-gặp-troubleshooting)

---

## 1. GIỚI THIỆU TỔNG QUAN

**Tool MYBAE AUTO (Hệ Thống Phân Tích Chất Lượng Bảo Trì Sài Gòn 01)** là giải pháp phần mềm tự động hóa toàn diện quy trình kiểm tra, đánh giá kỹ thuật và giám sát chất lượng dịch vụ Internet sau triển khai / bảo trì dành cho đội ngũ quản lý và kỹ thuật viên FPT Telecom.

### Mục tiêu cốt lõi:
- **Tự động hóa 100%** thao tác tra cứu thủ công từng hợp đồng trên trang điều hành kỹ thuật `management.mypt.vn`.
- **Phát hiện sớm và chính xác** các nguyên nhân gây rớt mạng lặp lại (CLL), suy hao công suất quang, bất thường tại tập điểm hoặc sóng Wi-Fi khách hàng suy giảm.
- **Tự động đối chiếu chéo chuyên sâu:** So sánh sự ổn định kết nối với mốc hoàn tất bảo trì, so sánh suy hao thuê bao với mức trung bình của toàn bộ tập điểm, truy vết thiết bị bắt sóng kém.
- **Tự động cập nhật (Auto Note) cảnh báo và hướng xử lý** trực tiếp vào từng phiếu bảo trì trên hệ thống điều hành FPT Inside (`inside.fpt.net`) mà không cần kỹ thuật viên phải thao tác thủ công.

---

## 2. KIẾN TRÚC KỸ THUẬT & TÍCH HỢP HỆ THỐNG

- **Backend:** Python 3.10+, nền tảng Flask Web Framework, chạy đa luồng (Multi-threading) xử lý tác vụ nền.
- **Tự động hóa trình duyệt:** Selenium WebDriver (Headless Chrome) với cơ chế phân tích văn bản động (Body Text Parsing & DOM Dynamic Wait), tự động xử lý popup và tái phân tích.
- **Cơ sở dữ liệu đám mây (Cloud Database):** Tích hợp REST API của **Supabase PostgREST**, lưu trữ và đồng bộ tập trung các bảng:
  - `bao_tri`: Danh sách ca bảo trì đã hoàn tất.
  - `ton_bao_tri`: Danh sách ca bảo trì đang tồn đọng.
  - `employees`: Danh bạ nhân viên kỹ thuật và phân bổ Đội Trưởng.
  - `cll30`, `kh_cls`: Dữ liệu khách hàng chăm sóc đặc biệt và kiểm soát chất lượng.
- **Xác thực 2 lớp FPT (2FA / TOTP):** Tự động đọc mã xác thực OTP qua giao thức IMAP từ Webmail FPT (`mail.fpt.net`) khi đăng nhập MyPT; tự động sinh mã Google Authenticator (TOTP RFC 6238) để vượt qua xác thực FPT Inside.
- **Truy cập từ xa (Remote Access):** Nhúng sẵn **Cloudflare Tunnel** (`cloudflared.exe`) tạo liên kết công khai an toàn để kỹ thuật viên truy cập từ xa qua điện thoại/máy tính ngoài mạng nội bộ.
- **Giao diện người dùng (Frontend):** Thiết kế Modern Glassmorphism theo tiêu chuẩn trực quan cao cấp, hỗ trợ Server-Sent Events (SSE) cập nhật tiến trình phân tích theo thời gian thực (real-time).

---

## 3. CHI TIẾT CÁCH TÍNH & QUY TẮC ĐÁNH GIÁ CÁC CHỈ SỐ PHÂN TÍCH CHUYÊN SÂU

### 3.1. Chẩn đoán hệ thống & Xử lý lỗi tự động
- **Nguồn dữ liệu:** Đọc từ tab **"Xử lý lỗi tự động"** trên giao diện chẩn đoán MyPT.
- **Xử lý khởi động lại modem:** Trong quá trình phân tích, hệ thống tự động kiểm tra xem có popup đề xuất *"Khởi động lại modem"* hay không.
  - Nếu có popup: Tự động bấm xác nhận Khởi động lại modem → Chờ hoàn tất → Bấm **Phân tích lại** lần 2 để có kết quả chính xác nhất.
  - Ghi nhận hành động `"Reboot modem"` vào cột **Xử lý tự động**.

---

### 3.2. Cảnh báo, Cần xử lý & Quy tắc đổi Modem WF6
- **Nguồn dữ liệu:** Đọc từ 2 tab **"Cần xử lý"** và **"Cảnh báo"**.
- **Quy tắc gộp hướng xử lý:** Toàn bộ hướng xử lý chi tiết (HXL) phát hiện từ tab Cảnh báo sẽ được tự động gộp vào cột **Cần xử lý** để kỹ thuật viên nắm rõ hành động khắc phục cụ thể.
- **Bộ lọc loại trừ cổng LAN:** Hệ thống tự động lọc bỏ sạch 100% các dòng cảnh báo/xử lý liên quan đến cổng LAN (ví dụ: *kiểm tra dây LAN, cắm lại cổng LAN...*) theo quy chuẩn vận hành không kiểm soát thiết bị cắm dây nội bộ của khách hàng.
- **Quy tắc nâng cấp Wi-Fi 6 đối với Tồn Bảo Trì:**
  - Áp dụng khi chọn nguồn dữ liệu là **Tồn Bảo Trì** (`ton_bao_tri`).
  - Nếu modem hiện hữu thuộc các dòng thiết bị chuẩn cũ (bắt đầu bằng ký tự **`AC`**, **`G`**, **`N`**, hoặc **`M`**):
  - Hệ thống tự động bổ sung hướng xử lý:
    $$\text{"Yêu cầu Swap WF6 Nâng Cao CLDV"}$$

---

### 3.3. Đánh giá Công suất thu Rx Power (Suy hao quang)
- **Nguồn dữ liệu:** Lấy từ trường **"Công suất thu"** (dBm) trong tab *Thông số hệ thống*.
- **Tiêu chuẩn kỹ thuật FPT:**
  $$\text{Chuẩn suy hao đạt: } -10.0\text{ dBm} \ge \text{Rx Power} \ge -23.5\text{ dBm}$$
- **Quy tắc phân loại và cảnh báo:**
  1. **Trường hợp đứt cáp / mất tín hiệu quang:**
     - Điều kiện: $\text{Rx Power} = 0.0\text{ dBm}$ hoặc $\le -1000\text{ dBm}$.
     - Cảnh báo: `"Cảnh báo đứt cáp/lỗi cáp (Công suất thu 0.0 dBm)"`.
     - Hướng xử lý: `"Hàn nối / xử lý đứt cáp, kiểm tra toàn bộ tuyến cáp quang"`.
  2. **Trường hợp suy hao ngoài chuẩn:**
     - Điều kiện: $\text{Rx Power} < -23.5\text{ dBm}$ (suy hao cao) HOẶC $\text{Rx Power} > -10.0\text{ dBm}$ (quá công suất phát).
     - Cảnh báo: `"Suy hao không đạt chuẩn (Công suất thu [X]dBm ngoài chuẩn -10 đến -23.5dBm)"`.
     - Hướng xử lý: `"Kiểm tra các thành phần: 1. Đầu FC modem 2. Cổng quang Modem/SFU 3. Cáp quang 4. Tập điểm"`.
  3. **Trường hợp đạt chuẩn:** Nằm trong khoảng $[-23.5, -10.0]\text{ dBm}$ $\rightarrow$ Đạt yêu cầu.

---

### 3.4. Đối chiếu Rớt Kết Nối (Disconnections Cross-Check)
Chỉ số này đánh giá độ ổn định thực tế của đường truyền, kết hợp dữ liệu giữa bảng **"Các lần kết nối"** và **"Nguyên nhân rớt kết nối OLT"**.

#### A. Điều kiện kích hoạt đối chiếu:
Thực hiện khi:
$$\text{Số lần rớt kết nối} \ge 1 \quad \text{HOẶC} \quad (\text{HĐ Tồn Bảo Trì} \ \text{VÀ} \ \text{Công suất thu} = 0.0\text{ dBm})$$

#### B. Cửa sổ thời gian đối chiếu:
- **Đối với ca Bảo Trì Hoàn Tất:** Đánh giá trong **24 giờ** tính từ mốc hoàn tất:
  $$\text{Cửa sổ đối chiếu} = [\text{TG Hoàn Tất}, \ \text{TG Hoàn Tất} + 24\text{ giờ}]$$
- **Đối với ca Tồn Bảo Trì:** Đánh giá trong **48 giờ** gần nhất tính từ thời điểm hiện tại:
  $$\text{Cửa sổ đối chiếu} = [\text{Hiện tại} - 48\text{ giờ}, \ \text{Hiện tại}]$$

#### C. Quy tắc tính thời gian mất mạng đối với HĐ Tồn bị 0.0 dBm:
- Hệ thống trích xuất mốc thời gian **"Ra Mạng sau cùng"** (dòng đầu tiên của bảng Các lần kết nối).
- Tính khoảng cách thời gian: $\Delta t = \text{Hiện tại} - \text{Thời gian Ra Mạng sau cùng}$.
- Định dạng hiển thị trực quan:
  $$\text{"KH đã Ra Mạng [X ngày Y giờ Z phút] (Lần Ra Mạng sau cùng: DD/MM/YYYY HH:MM:SS)"}$$
- Nếu không có dòng lịch sử kết nối: Tự động tính thời gian từ lúc tạo phiếu tồn bảo trì:
  $$\text{"Phiếu bảo trì đã tồn [X ngày Y giờ Z phút] (Không có data Các lần kết nối)"}$$

#### D. Thuật toán kết luận Rớt Mạng:
Đếm số sự kiện ngắt mạng hợp lệ ($N_{\text{ra}}$) trong cửa sổ thời gian và đối chiếu nguyên nhân OLT:
1. **$N_{\text{ra}} > 2$ lần:**
   $$\rightarrow \text{"Chưa đảm bảo (Trong [24H/48H] phát hiện } N_{\text{ra}} \text{ lần Ra Mạng - NN OLT: [Nguyên nhân])"}$$
2. **$N_{\text{ra}} = 2$ lần:**
   - Nếu nguyên nhân ghi nhận từ OLT là **`POWER_OFF`**: Đánh giá **`Đảm bảo`** (do khách hàng chủ động tắt/bật nguồn thiết bị):
     $$\rightarrow \text{"Đảm bảo (Trong [24H/48H] rớt 2 lần do POWER_OFF)"}$$
   - Nếu nguyên nhân khác (ví dụ: `LOSi`, `LINK_DOWN`, `DYING_GASP`...):
     $$\rightarrow \text{"Chưa đảm bảo (Trong [24H/48H] phát hiện 2 lần Ra Mạng - NN OLT: [Nguyên nhân])"}$$
3. **$N_{\text{ra}} = 1$ lần:**
   $$\rightarrow \text{"Đảm bảo (Trong [24H/48H] chỉ ra Mạng 1 Lần)"}$$
4. **$N_{\text{ra}} = 0$ lần:**
   $$\rightarrow \text{"Đảm bảo (Trong [24H/48H] kết nối ổn định)"}$$

---

### 3.5. Đối chiếu Hợp đồng cùng Tập Điểm (Tap-Point Cross-Check)
Chức năng này quét toàn bộ các thuê bao đang cùng đấu nối trên một hộp cáp tập điểm ngoài hiện trường để xác định lỗi cục bộ thuê bao hay sự cố diện rộng của hộp tập điểm.

#### A. Thu thập và trích xuất chỉ số tập điểm:
- Quét toàn bộ các trang dữ liệu của sub-tab *Hợp đồng cùng tập điểm*.
- Đếm:
  - $T$: Tổng số thuê bao cùng tập điểm.
  - $N_{\text{online}}$: Số thuê bao đang Online.
  - $N_{\text{offline}}$: Số thuê bao đang Offline.
  - $N_{\text{kđ}}$: Số thuê bao có Rx Power ngoài chuẩn ($< -23.5\text{ dBm}$ hoặc $> -10.0\text{ dBm}$).
  - $N_{\text{đứt}}$: Số thuê bao đứt cáp ($0.0\text{ dBm}$ hoặc $\le -1000\text{ dBm}$).
  - $N_{\text{un}}$: Số thuê bao trạng thái Unknown/không đo được.

#### B. Công thức tính Công Suất Trung Bình Tập Điểm ($P_{\text{TB}}$):
Chỉ lấy các thuê bao đang **Online** và có công suất **nằm trong chuẩn kỹ thuật** để tính trung bình đại diện cho hộp tập điểm:
$$P_{\text{TB}} = \frac{\sum_{i=1}^{M} P_i}{M} \quad \text{với } P_i \in [-23.5, -10.0]\text{ dBm và đang Online}$$

#### C. So sánh công suất HĐ hiện tại ($P_{\text{HĐ}}$) với tập điểm:
- Nếu $P_{\text{HĐ}} = 0.0\text{ dBm}$: Tiền tố kết luận: `"Cảnh báo đứt cáp/lỗi cáp (0.0dBm) | "`
- Nếu $P_{\text{HĐ}} \ge P_{\text{TB}}$ và đạt chuẩn: Tiền tố kết luận: `"Đạt (HĐ [P_HĐ]dBm / TB Tdiem [P_TB]dBm) | "`
- Nếu $P_{\text{HĐ}} < P_{\text{TB}}$ hoặc ngoài chuẩn: Tiền tố kết luận: `"Chưa đạt (HĐ [P_HĐ]dBm / TB Tdiem [P_TB]dBm) | "`

#### D. Phân loại tình trạng sức khỏe tập điểm:
1. **Trường hợp Cảnh báo tập điểm diện rộng:**
   $$\text{Tỷ lệ Offline} = \frac{N_{\text{offline}}}{T} \ge 50\%$$
   $$\rightarrow \text{"Cảnh báo tập điểm (Tập điểm này có } T \text{ HĐ; } N_{\text{offline}}/T \text{ Offline (>=50\%)...)"}$$
2. **Trường hợp tập điểm có suy hao ngoài chuẩn:**
   - Khi $N_{\text{kđ}} > 0$ hoặc $N_{\text{đứt}} > 0$:
   $$\rightarrow \text{"Yêu cầu xử lý Rx Power (Tập điểm này có } T \text{ HĐ; } N_{\text{kđ}}/T \text{ HĐ không đạt chuẩn...)"}$$
3. **Trường hợp có HĐ Unknown:** Khi $N_{\text{un}} > 0 \rightarrow$ `"Tập điểm có HĐ Unknown..."`.
4. **Trường hợp có HĐ Offline đơn lẻ ($< 50\%$):** $\rightarrow$ `"Tập điểm có HĐ Offline..."`.
5. **Trường hợp tập điểm chuẩn hoàn toàn:** $\rightarrow$ `"Tập điểm bình thường (Tập điểm này có } T \text{ HĐ; } N_{\text{online}} \text{ Online; Rx Power đạt chuẩn)"`.

---

### 3.6. Phân tích Thiết bị Kết Nối Kém (Client Analysis)
- **Điều kiện kích hoạt:** Khi trong cảnh báo của modem xuất hiện các từ khóa: `"sóng wifi yếu"`, `"thu phát kém"`, `"kết nối kém"`.
- **Hành động tự động:**
  1. Chuyển sang tab **"Lịch sử thiết bị kết nối kém"**.
  2. Xác định ngày gần nhất ghi nhận lỗi trong bảng lịch sử.
  3. Đọc số lượng thiết bị phân trang (ví dụ: `1-5 of 12` $\rightarrow$ 12 thiết bị).
  4. Trích xuất danh sách địa chỉ MAC của điện thoại/máy tính cùng mức tín hiệu suy hao RSSI (dBm).
- **Định dạng kết quả:**
  $$\text{"Ngày DD/MM/YYYY: [X] thiết bị kém [a1:b2:c3:d4:e5 (-86dBm), f6:g7:h8:i9:k0 (-88dBm)...]"}$$

---

### 3.7. Chuẩn hóa & Cô đọng nội dung Auto Note lên FPT Inside
Hệ thống FPT Inside giới hạn độ dài mỗi lần ghi chú không quá 450 - 500 ký tự. Hệ thống áp dụng thuật toán tối ưu hóa thông minh:
1. **Lược bỏ thông tin bình thường:**
   - Ẩn đối chiếu Rớt kết nối nếu kết quả là `Đảm bảo`.
   - Ẩn đối chiếu Tập điểm nếu kết quả là `Đạt` hoặc `Tập điểm bình thường`.
   - Ẩn phân tích Client nếu không có thiết bị suy hao.
2. **Cắt gọn ký tự dư thừa:** Viết tắt các cụm từ dài (ví dụ: `Kiểm tra các thành phần sau` $\rightarrow$ loại bỏ; `Tư vấn lắp AP` $\rightarrow$ `Tư vấn AP`).
3. **Cấu trúc 1 Note tiêu chuẩn ($\le 450$ ký tự):**
   ```text
   1. ⚠️ CẢNH BÁO: Suy hao không đạt chuẩn...
   2. 🔧 CẦN XỬ LÝ: Kiểm tra các thành phần: 1. Đầu FC...
   3. 🌐 SUY HAO: Chưa đạt (HĐ -24.5dBm / TB Tdiem -19.2dBm)
   4. 📶 THÔNG SỐ: Rx: -24.5dBm | 📉 Rớt: 0 | 📦 G-97RG6M
   ```
4. **Cơ chế tách 2 Note:** Nếu bắt buộc phải ghi chú đầy đủ nhiều mục lỗi mà vượt quá 450 ký tự, hệ thống tự động tách thành 2 Note riêng biệt, giữ nguyên số thứ tự nối tiếp (1, 2, 3 và 4, 5, 6) để không bị đứt đoạn thông tin.
5. **Cơ chế loại trừ tự động trong 24 giờ:** Các hợp đồng Tồn Bảo Trì đã được phân tích hoặc đã bắn Auto Note thành công trong vòng 24 giờ qua sẽ tự động được bỏ qua ở các lần quét tiếp theo, tránh tình trạng bắn ghi chú trùng lặp.

---

## 4. CÁC CHỨC NĂNG CHÍNH TRÊN GIAO DIỆN WEB

Giao diện Web của hệ thống được tổ chức thành 5 tab điều hướng chuyên biệt:

1. **Tab 1: Phân Tích Hợp Đồng**
   - Lọc theo khoảng ngày hoàn tất (Presets: *Hôm nay*, *3 ngày gần nhất*, *7 ngày*, *Tháng này*).
   - Lựa chọn nguồn dữ liệu: **Bảo Trì** (Hợp đồng đã bảo trì) hoặc **Tồn Bảo Trì** (Hợp đồng chưa xử lý kèm Auto Note).
   - Lọc theo **Đội Trưởng** hoặc chọn đa nhân viên kỹ thuật.
   - **Cấu hình MAP Đội Trưởng - Nhân Viên:** Hỗ trợ nạp file Excel/CSV (2 cột: Đội Trưởng, Nhân Viên/Inside Account), tạo/sửa/xóa Đội Trưởng và gán nhân viên trực quan ngay trên giao diện web. Tự động lưu bền vững và đồng bộ theo từng Chi Nhánh.
   - Hiển thị tiến trình chẩn đoán Real-time từng bước (Step 1 $\rightarrow$ Step 6).
   - Bảng kết quả chẩn đoán chi tiết chia thành 5 cụm thông tin:
     - *Thông tin cơ bản* (Số HĐ, Nhân viên, Ngày HT).
     - *Kết quả chẩn đoán* (Xử lý tự động, Cần xử lý, Cảnh báo).
     - *Thông số hệ thống* (Công suất thu, Số lần rớt).
     - *Thông số modem* (Loại modem, Firmware, DNS WAN).
     - *Đối chiếu chuyên sâu* (Rớt mạng, Tập điểm, Thiết bị client kém).
   - Xuất dữ liệu ra file Excel / CSV nhanh chóng.

2. **Tab 2: Lịch Sử Chạy**
   - Lưu trữ toàn bộ các phiên phân tích trước đó.
   - Cho phép xem lại danh sách kết quả, lọc hoặc xuất báo cáo lại bất cứ lúc nào.

3. **Tab 3: Thống Kê Cảnh Báo (KPI Dashboard)**
   - Biểu đồ phân nhóm lỗi: *Suy hao quang*, *Rớt kết nối nhiều*, *Lỗi DNS WAN*, *Tự động xử lý*, *Cần kỹ thuật xử lý*.
   - **Bảng xếp hạng chất lượng theo Nhân viên (Leaderboard):** Thống kê số lượng hợp đồng có lỗi, tỷ lệ suy hao theo từng kỹ thuật viên để đội trưởng giám sát và đào tạo nghiệp vụ.

4. **Tab 4: Import / Data (Supabase) - Phân Quyền & Bảo Toàn Dữ Liệu Theo Chi Nhánh**
   - **Cách ly dữ liệu theo Chi Nhánh:** Khi import, hệ thống yêu cầu chọn hoặc đặt tên Chi Nhánh. Dữ liệu của từng chi nhánh được đóng dấu và lưu trữ riêng biệt.
   - **Import 2 phần chuẩn:**
     - *Phần 1: Bảng Bảo Trì (`bao_tri`)* - Nhập danh sách bảo trì FTTH hoàn tất trong kỳ.
     - *Phần 2: Bảng Tồn Bảo Trì (`ton_bao_tri`)* - Nhập ca tồn. Hệ thống tự động làm mới riêng cho chi nhánh này, **hoàn toàn không làm mất hay ảnh hưởng dữ liệu của các chi nhánh khác**.
   - Hỗ trợ tạo Chi Nhánh mới (`+ Đặt Tên Chi Nhánh Mới`), ghi nhớ chi nhánh cho các lần import sau.
   - Quản lý và lọc dữ liệu trực tiếp theo từng Chi Nhánh trong bảng Supabase.

5. **Tab 5: Auto Note Tồn Bảo Trì**
   - Tự động đăng nhập hệ thống FPT Inside với cơ chế **Google Authenticator (TOTP)** tự sinh mã 6 số.
   - Đọc danh sách hợp đồng cần ghi chú từ kết quả phân tích gần nhất.
   - Hiển thị Live Terminal log chi tiết từng thao tác click chuột, mở tab, điền nội dung và bấm cập nhật.
   - Hỗ trợ tải lên ảnh mã QR để giải mã và lưu trữ Secret Key TOTP tự động.

---

## 5. HƯỚNG DẪN CÀI ĐẶT & VẬN HÀNH HỆ THỐNG

### 5.1. Yêu cầu hệ thống
- Hệ điều hành: Windows 10/11 hoặc Windows Server.
- Trình duyệt: **Google Chrome** phiên bản mới nhất.
- Môi trường: Python 3.10 trở lên.
- Kết nối mạng ổn định (truy cập được `management.mypt.vn` và `inside.fpt.net`).

### 5.2. Cấu hình thông tin ban đầu (`config.py`)
Mở file [config.py](file:///d:/Tool%20MYBAE%20AUTO/config.py) và cấu hình các thông số:
```python
# Tài khoản đăng nhập MyPT (Mail FPT)
EMAIL = 'phuongnam.phongnh5@fpt.net'
EMAIL_PASSWORD = 'Benngo@@2026'

# Tài khoản đăng nhập FPT Inside
INSIDE_ACCOUNT  = 'phuongnam.phongnh5'
INSIDE_PASSWORD = 'Benngo@@2026'

# Khóa bí mật Google Authenticator TOTP (Base32)
TOTP_SECRET = 'J5MBXLD2IZJSJWADQU'

# Khóa cơ sở dữ liệu Supabase
SUPABASE_URL = 'https://zqkithrewcorqyckfdrr.supabase.co'
SUPABASE_KEY = 'sb_publishable_...'
```

### 5.3. Khởi chạy ứng dụng
- **Cách 1 (Nhanh nhất):** Nhấp đúp chuột vào file [run.bat](file:///d:/Tool%20MYBAE%20AUTO/run.bat).
- **Cách 2:** Mở Terminal / PowerShell tại thư mục dự án và chạy:
  ```powershell
  python app.py
  ```
- Trình duyệt sẽ tự động mở trang Dashboard tại địa chỉ: `http://localhost:5000` (hoặc mở đường link Cloudflare Tunnel hiển thị trên đầu trang để truy cập từ xa).

### 5.4. Quy trình vận hành chuẩn hàng ngày
1. **Bước 1: Đăng nhập MyPT:** Bấm nút **"Đăng nhập hệ thống"** ở góc phải trên cùng $\rightarrow$ Hệ thống tự động mở trình duyệt, nhập mật khẩu và tự đọc mã OTP từ Webmail FPT để đăng nhập.
2. **Bước 2: Chuẩn bị dữ liệu theo Chi Nhánh:**
   - Vào tab **Import / Data (Supabase)**.
   - **Xác nhận Chi Nhánh:** Chọn chi nhánh của bạn từ dropdown hoặc bấm **"+ Đặt Tên Chi Nhánh Mới"** nếu là chi nhánh mới.
   - **Chọn Phần cần Import (2 phần):** Chọn *Phần 1: Bảng Bảo Trì* hoặc *Phần 2: Bảng Tồn Bảo Trì*.
   - **Tải file:** Kéo thả file Excel/CSV vào ô tải lên $\rightarrow$ Bấm **"Tải Lên Supabase"**.
   - *An toàn tuyệt đối:* Dữ liệu của bạn được cách ly độc lập, không bị đè hay làm mất dữ liệu của các chi nhánh khác.
3. **Bước 3: Chạy phân tích:**
   - Chọn khoảng ngày hoặc chọn Đội trưởng / Nhân viên cần kiểm tra.
   - Chọn nguồn dữ liệu: *Hợp đồng Bảo Trì* hoặc *Hợp đồng Tồn Bảo Trì*.
   - Bấm **"Bắt Đầu Phân Tích"** và theo dõi tiến trình chạy live.
4. **Bước 4: Kiểm tra kết quả:** Xem các hợp đồng hiển thị màu đỏ (Cần xử lý) hoặc vàng (Cảnh báo), kiểm tra các cột đối chiếu Rớt mạng và Tập điểm. Bấm **Xuất CSV** để lưu file báo cáo.
5. **Bước 5: Chạy Auto Note (Dành cho ca Tồn):**
   - Chuyển sang tab **"Auto Note Tồn BT"**.
   - Bấm nút **"Kiểm Tra OTP Hiện Tại"** để chắc chắn mã 6 số TOTP khớp với điện thoại.
   - Bấm **"Làm mới từ kết quả phân tích"** $\rightarrow$ Bấm **"Bắt Đầu Auto Note"**.
   - Theo dõi màn hình Live Log đến khi hoàn tất 100%.

---

## 6. XỬ LÝ SỰ CỐ THƯỜNG GẶP (TROUBLESHOOTING)

| Tình huống sự cố | Nguyên nhân khả dĩ | Hướng dẫn khắc phục |
| :--- | :--- | :--- |
| **Không đăng nhập được MyPT (Báo timeout OTP)** | Webmail FPT gửi OTP chậm hoặc mật khẩu mail bị đổi. | 1. Kiểm tra lại mật khẩu mail trong `config.py`.<br>2. Thử mở trình duyệt đăng nhập thủ công vào `mail.fpt.net` để kiểm tra hòm thư có bị đầy hay không. |
| **Báo lỗi mã TOTP FPT Inside không đúng** | Đồng hồ máy tính chạy tool bị lệch giờ so với giờ chuẩn internet. | 1. Vào Cài đặt Windows $\rightarrow$ **Date & Time** $\rightarrow$ Bấm **"Sync now"** để đồng bộ lại giây.<br>2. Bấm nút *Kiểm Tra OTP Hiện Tại* trên tab Auto Note và so khớp với ứng dụng Google Authenticator trên điện thoại. |
| **Bảng tập điểm báo "Không đọc được danh sách"** | Server FPT phản hồi chậm khi tải dữ liệu Hợp đồng cùng tập điểm. | Hệ thống đã có cơ chế Dynamic Wait tối đa 35 giây. Nếu mạng nội bộ FPT quá tải, hãy thử chạy lại vào thời điểm khác hoặc kiểm tra băng thông mạng. |
| **Trình duyệt Chrome bị kẹt / chiếm dụng RAM** | Các tiến trình Chrome chạy ngầm cũ chưa đóng hết khi tắt ngang tool. | Mở Task Manager (Ctrl + Shift + Esc), tìm và End Task các tiến trình có tên `chromedriver.exe` hoặc `chrome.exe`, sau đó khởi động lại `run.bat`. |
| **Báo lỗi Supabase Request Failed** | Khóa API Key của Supabase đã hết hạn hoặc bị đổi. | Vào tab **Import / Data (Supabase)** $\rightarrow$ Dán Supabase Key mới vào ô cấu hình $\rightarrow$ Bấm **Lưu Khóa**. |

---
*Tài liệu kỹ thuật được biên soạn phục vụ vận hành và bàn giao Hệ Thống Phân Tích Chất Lượng Bảo Trì Sài Gòn 01.*
