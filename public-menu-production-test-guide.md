# Hướng Dẫn Test Public Menu API Trên Production

## 1. Mục tiêu

Tài liệu này dùng để gửi cho đối tác hoặc team QA test nhanh 2 API public menu đang chạy trên production.

Base URL production:

```text
https://api-coffee.8am.vn
```

Hai API cần test:

1. `GET /public/v1/menu`
2. `GET /public/v1/menu/detail`

Các API này là public read-only:

- không cần đăng nhập
- không cần token
- không cần đăng ký tài khoản

---

## 2. API 1: Lấy danh sách item menu gộp

### Endpoint

```http
GET https://api-coffee.8am.vn/public/v1/menu
```

### Mục đích

API này trả về danh sách item gộp từ nhiều nguồn:

- hạt cà phê
- đồ uống đóng chai
- dụng cụ cà phê
- máy xay
- brewer / máy pha
- món dish

Mỗi item chỉ trả thông tin gọn:

- `id`
- `name`
- `type`

### Query hỗ trợ

| Query | Bắt buộc | Ý nghĩa |
|---|---|---|
| `name` | không | Lọc theo tên, dạng contains, không phân biệt hoa thường/dấu |
| `type` | không | Lọc theo loại item |

### Giá trị `type` hợp lệ

```text
coffee
bottledDrink
coffeeEquipment
grinder
brewer
dish
```

### Ví dụ

Lấy toàn bộ item:

```bash
curl --location 'https://api-coffee.8am.vn/public/v1/menu'
```

Lọc theo tên:

```bash
curl --location 'https://api-coffee.8am.vn/public/v1/menu?name=cà%20phê'
```

Lọc theo loại:

```bash
curl --location 'https://api-coffee.8am.vn/public/v1/menu?type=coffee'
```

Lọc theo tên + loại:

```bash
curl --location 'https://api-coffee.8am.vn/public/v1/menu?name=arabica&type=coffee'
```

### Response mẫu

```json
{
  "success": true,
  "generatedAt": "2026-04-29T12:55:14.137Z",
  "version": "v1",
  "items": [
    {
      "id": "Ca0trKQgQP0EgaI6PFe0",
      "name": "CÀ PHÊ MUỐI",
      "type": "dish"
    },
    {
      "id": "coffee-123",
      "name": "Cà phê House Blend",
      "type": "coffee"
    }
  ]
}
```

---

## 3. API 2: Lấy chi tiết item theo `id` hoặc `name`

### Endpoint

```http
GET https://api-coffee.8am.vn/public/v1/menu/detail
```

### Mục đích

API này trả về chi tiết item matching từ menu gộp.

Có thể tìm theo:

- `id`
- `name`

Nếu truyền cả `id` và `name`:

- API sẽ ưu tiên `id`

### Query hỗ trợ

| Query | Bắt buộc | Ý nghĩa |
|---|---|---|
| `id` | không | ID chính xác của item |
| `name` | không | Tên item để tìm theo contains |
| `type` | không | Giới hạn tìm trong một loại |

Lưu ý:

- phải truyền ít nhất `id` hoặc `name`
- `type` là optional nhưng nên truyền nếu muốn lọc chính xác hơn

### Ví dụ

Lấy chi tiết theo `id`:

```bash
curl --location 'https://api-coffee.8am.vn/public/v1/menu/detail?id=Ca0trKQgQP0EgaI6PFe0'
```

Lấy chi tiết theo tên:

```bash
curl --location 'https://api-coffee.8am.vn/public/v1/menu/detail?name=cà%20phê'
```

Lấy chi tiết theo tên và loại:

```bash
curl --location 'https://api-coffee.8am.vn/public/v1/menu/detail?name=cà%20phê&type=dish'
```

Lấy chi tiết theo `id` và loại:

```bash
curl --location 'https://api-coffee.8am.vn/public/v1/menu/detail?id=Ca0trKQgQP0EgaI6PFe0&type=dish'
```

### Response mẫu

```json
{
  "items": [
    {
      "type": "dish",
      "detail": {
        "id": "Ca0trKQgQP0EgaI6PFe0",
        "code": "D-01",
        "name": "CÀ PHÊ MUỐI",
        "price": 45000,
        "unit": "ly",
        "description": "Cà phê muối",
        "isActive": true
      }
    }
  ]
}
```

Lưu ý:

- `items[]` có thể chứa nhiều kết quả nếu tìm theo `name`
- nếu tìm theo `id` thông thường sẽ ra 1 kết quả
- `detail` sẽ khác nhau tùy theo `type`

---

## 4. Mã lỗi cần biết

### `200 OK`

Request thành công.

### `400 Bad Request`

Một số trường hợp:

- không truyền cả `id` lẫn `name` cho API detail
- truyền `type` sai giá trị

Ví dụ:

```json
{
  "success": false,
  "message": "Query \"id\" or \"name\" is required"
}
```

### `404 Not Found`

Không tìm thấy item phù hợp.

Ví dụ:

```json
{
  "success": false,
  "message": "Menu item not found"
}
```

### `500 Internal Server Error`

Lỗi hệ thống hoặc lỗi truy vấn dữ liệu.

---

## 5. Bộ test khuyến nghị

### Test API list

1. Gọi `/public/v1/menu` không query
2. Gọi `/public/v1/menu?name=cà phê`
3. Gọi `/public/v1/menu?type=coffee`
4. Gọi `/public/v1/menu?name=cà phê&type=dish`

Kỳ vọng:

- response `200`
- có `items[]`
- mỗi item có `id`, `name`, `type`

### Test API detail

1. Copy một `id` từ API list rồi gọi `/public/v1/menu/detail?id=...`
2. Gọi `/public/v1/menu/detail?name=cà phê`
3. Gọi `/public/v1/menu/detail?name=cà phê&type=dish`
4. Gọi `/public/v1/menu/detail?id=...&type=dish`

Kỳ vọng:

- response `200` nếu item tồn tại
- `items[]` chứa detail đúng với item cần tìm
- nếu truyền `id` hợp lệ thì kết quả phải ra đúng item đó

### Test lỗi

1. Gọi `/public/v1/menu/detail` không có query
2. Gọi `/public/v1/menu?type=abc`
3. Gọi `/public/v1/menu/detail?id=not-found-id`

Kỳ vọng:

- trả `400` cho query sai
- trả `404` cho item không tồn tại

---

## 6. Ghi chú cho bên tích hợp

1. Nên gọi API list trước để lấy `id`.
2. Khi cần chi tiết chính xác, nên dùng `id` thay vì `name`.
3. Tìm theo `name` phù hợp cho search gần đúng.
4. `type` nên được truyền kèm khi cần giới hạn kết quả.
5. Đây là API public nên chỉ dùng cho đọc dữ liệu, không có chức năng ghi hoặc cập nhật.

---

## 7. Tóm tắt nhanh

### Danh sách item

```http
GET https://api-coffee.8am.vn/public/v1/menu
```

### Chi tiết item

```http
GET https://api-coffee.8am.vn/public/v1/menu/detail?id={id}
```

hoặc

```http
GET https://api-coffee.8am.vn/public/v1/menu/detail?name={keyword}
```
