// Package objectstore 是对象存储的全部访问。
//
// # 唯一的设计约束：字节流不进本进程
//
// 不变式 6 ——「大文件不得完整进入应用进程内存，也不得由应用进程长期中转
// 下载流量」。所以这个包里**没有 Put/Get 字节流的方法**，只有：
//
//   - 签发 multipart 上传的分片 URL（客户端直传）
//   - complete/abort multipart
//   - 查对象的大小与 ETag（HEAD，不下载）
//   - 签发短期下载 URL（浏览器直读，支持 Range）
//   - 流式校验摘要（后台，边读边算，常数内存）
//
// 最后一条是唯一会读字节的地方，它跑在后台校验器里、按块读、不留缓冲。
package objectstore

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net/url"
	"time"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

type Store struct {
	client       *minio.Client
	publicClient *minio.Client // 用浏览器可达的 endpoint 签名
	bucket       string
	presignTTL   time.Duration
}

type Config struct {
	Endpoint       string
	PublicEndpoint string
	AccessKey      string
	SecretKey      string
	Bucket         string
	Secure         bool
	// 公网那一侧的 scheme，见 config.ObjectPublicSecure
	PublicSecure bool
	Region       string
	PresignTTL   time.Duration
}

func Open(ctx context.Context, c Config) (*Store, error) {
	// **Region 必须显式给**。不给的话 minio-go 在签名前会先向该 endpoint
	// 发一次 `GET /{bucket}/?location=` 去问区域 —— 而给浏览器签名用的那个
	// client 指的是**浏览器可达**的地址（127.0.0.1:19000），
	// 容器里根本连不上它。表现是 `POST /api/uploads` 502 objectstore_error，
	// 而启动自检（走内网 client）一切正常。
	//
	// 这个坑只在"内外两个 endpoint"的部署形态下出现，本机单测与
	// 进程内 e2e 都碰不到 —— 2026-09-02 第一次真起全栈时炸出来的。
	mk := func(endpoint string, secure bool) (*minio.Client, error) {
		return minio.New(endpoint, &minio.Options{
			Creds:  credentials.NewStaticV4(c.AccessKey, c.SecretKey, ""),
			Secure: secure,
			Region: c.Region,
		})
	}
	internal, err := mk(c.Endpoint, c.Secure)
	if err != nil {
		return nil, err
	}
	// **内外两个 endpoint 必须分开**：服务自己走内网地址，
	// 而签给浏览器的 URL 必须是浏览器解析得了的主机名。
	// 用同一个的话，容器里签出来的 URL 里带着 `minio:9000`，
	// 浏览器一访问就是 DNS 失败 —— 而这只在真部署里才暴露
	//
	// **scheme 也可能内外不同**：内网走回环明文、公网由隧道终结 TLS 时，
	// 两个 endpoint 可以是同一个 host（都经反代），但签名必须签成 https。
	// 所以这里的判据是"endpoint 或 scheme 任意一个不同"，不是只看 endpoint
	public := internal
	if (c.PublicEndpoint != "" && c.PublicEndpoint != c.Endpoint) || c.PublicSecure != c.Secure {
		endpoint := c.PublicEndpoint
		if endpoint == "" {
			endpoint = c.Endpoint
		}
		if public, err = mk(endpoint, c.PublicSecure); err != nil {
			return nil, err
		}
	}
	s := &Store{client: internal, publicClient: public, bucket: c.Bucket, presignTTL: c.PresignTTL}

	ok, err := internal.BucketExists(ctx, c.Bucket)
	if err != nil {
		return nil, fmt.Errorf("对象存储连不上：%w", err)
	}
	if !ok {
		if err := internal.MakeBucket(ctx, c.Bucket, minio.MakeBucketOptions{}); err != nil {
			return nil, err
		}
	}
	return s, nil
}

func (s *Store) Bucket() string { return s.bucket }

func (s *Store) Ping(ctx context.Context) error {
	_, err := s.client.BucketExists(ctx, s.bucket)
	return err
}

// ---------------------------------------------------------- 直传（§9.1）

type Part struct {
	PartNumber int    `json:"part_number"`
	URL        string `json:"url"`
}

// CreateMultipart 开一个 multipart 上传并签出全部分片 URL。
//
// 一次性把所有分片都签出来（而不是让客户端一片片来要），是因为
// 每要一次就是一次到控制面的往返，而 200MB / 16MB = 13 片 ——
// 13 次往返换 13 个 URL，不划算。TTL 由 PRESIGN_TTL 控制。
func (s *Store) CreateMultipart(ctx context.Context, key, contentType string,
	size, partSize int64) (uploadID string, parts []Part, err error) {

	core := minio.Core{Client: s.client}
	uploadID, err = core.NewMultipartUpload(ctx, s.bucket, key,
		minio.PutObjectOptions{ContentType: contentType})
	if err != nil {
		return "", nil, err
	}
	count := (size + partSize - 1) / partSize
	if count == 0 {
		count = 1
	}
	for i := int64(1); i <= count; i++ {
		q := url.Values{}
		q.Set("uploadId", uploadID)
		q.Set("partNumber", fmt.Sprint(i))
		u, err := s.publicClient.Presign(ctx, "PUT", s.bucket, key, s.presignTTL, q)
		if err != nil {
			// 签到一半失败就把这次 multipart 撤掉，别留下计费中的碎片
			_ = core.AbortMultipartUpload(ctx, s.bucket, key, uploadID)
			return "", nil, err
		}
		parts = append(parts, Part{PartNumber: int(i), URL: u.String()})
	}
	return uploadID, parts, nil
}

type CompletedPart struct {
	PartNumber int    `json:"part_number"`
	ETag       string `json:"etag"`
}

// CompleteMultipart 合并分片。
//
// 客户端没报 ETag 时**从对象存储自己列**：信客户端报的 ETag 等于
// 让客户端决定合并出什么东西。列一次的代价远低于那个风险。
func (s *Store) CompleteMultipart(ctx context.Context, key, uploadID string,
	reported []CompletedPart) error {

	core := minio.Core{Client: s.client}
	var parts []minio.CompletePart
	if len(reported) > 0 {
		for _, p := range reported {
			parts = append(parts, minio.CompletePart{PartNumber: p.PartNumber, ETag: p.ETag})
		}
	} else {
		listed, err := core.ListObjectParts(ctx, s.bucket, key, uploadID, 0, 10000)
		if err != nil {
			return err
		}
		for _, p := range listed.ObjectParts {
			parts = append(parts, minio.CompletePart{PartNumber: p.PartNumber, ETag: p.ETag})
		}
	}
	_, err := core.CompleteMultipartUpload(ctx, s.bucket, key, uploadID, parts,
		minio.PutObjectOptions{})
	return err
}

func (s *Store) AbortMultipart(ctx context.Context, key, uploadID string) error {
	core := minio.Core{Client: s.client}
	return core.AbortMultipartUpload(ctx, s.bucket, key, uploadID)
}

// Stat 只发 HEAD，不下载。finalize 时用它核对**真实**大小。
func (s *Store) Stat(ctx context.Context, key string) (size int64, etag string, err error) {
	info, err := s.client.StatObject(ctx, s.bucket, key, minio.StatObjectOptions{})
	if err != nil {
		return 0, "", err
	}
	return info.Size, info.ETag, nil
}

// Digest 流式算 sha256。
//
// **常数内存**：按 1MiB 缓冲读，不管文件多大。这是本包里唯一会读字节的地方，
// 它跑在后台校验器里 —— 请求路径上一个字节都不读。
func (s *Store) Digest(ctx context.Context, key string) (string, int64, error) {
	obj, err := s.client.GetObject(ctx, s.bucket, key, minio.GetObjectOptions{})
	if err != nil {
		return "", 0, err
	}
	defer obj.Close()

	h := sha256.New()
	n, err := io.CopyBuffer(h, obj, make([]byte, 1<<20))
	if err != nil {
		return "", 0, err
	}
	return hex.EncodeToString(h.Sum(nil)), n, nil
}

// PresignGet 签一个短期下载 URL。
//
// `disposition` 决定浏览器是内联预览还是下载。**MIME 白名单由调用方把关** ——
// 上传 text/html 并 inline 打开就是本站同源 XSS（旧系统 `/files` 的铁律 6）。
// PresignGet 签给**浏览器**的下载地址（用浏览器可达的 endpoint）。
func (s *Store) PresignGet(ctx context.Context, key, docID, mime, disposition string) (string, time.Time, error) {
	return s.presignGetWith(s.publicClient, ctx, key, docID, mime, disposition)
}

// PresignGetInternal 签给**其它服务**的下载地址（用内网 endpoint）。
//
// 两条必须分开。稳定文件 URL（`/files/{token}`）的消费者是 model-gateway ——
// 一个容器里的进程，它解析不了 `127.0.0.1:19000`（那是给浏览器的）。
// 用公网地址签的话，表现是解析任务 `failed: All connection attempts failed`，
// 而上传、入库、状态查询全都正常 —— 2026-09-02 真起全栈时炸出来的。
func (s *Store) PresignGetInternal(ctx context.Context, key, docID, mime, disposition string) (string, time.Time, error) {
	return s.presignGetWith(s.client, ctx, key, docID, mime, disposition)
}

func (s *Store) presignGetWith(client *minio.Client, ctx context.Context, key, filename, mime, disposition string) (string, time.Time, error) {
	q := url.Values{}
	if disposition != "" {
		q.Set("response-content-disposition",
			fmt.Sprintf("%s; filename*=UTF-8''%s", disposition, url.PathEscape(filename)))
	}
	if mime != "" {
		q.Set("response-content-type", mime)
	}
	u, err := client.PresignedGetObject(ctx, s.bucket, key, s.presignTTL, q)
	if err != nil {
		return "", time.Time{}, err
	}
	return u.String(), time.Now().Add(s.presignTTL), nil
}

// Remove 删对象。
// **调用方必须先 claim**（宽限期 + 条件 UPDATE）—— 这是全项目唯一
// 会不可逆毁数据的地方，corpus 侧的 gc.py 有同样的两道防护。
func (s *Store) Remove(ctx context.Context, key string) error {
	return s.client.RemoveObject(ctx, s.bucket, key, minio.RemoveObjectOptions{})
}
