# S3 checksum compatibility with OBS and other S3-compatible stores

**Read this if you upload to OBS (or any non-AWS S3-compatible store) with a
recent AWS SDK.** It will silently corrupt your objects until you turn one thing
off. This is not specific to this tool. It affects any application or script
using a current AWS SDK against these stores.

## Symptom

An upload "succeeds" with a 200, but the stored object is larger than what you
sent and its bytes do not match. A 1500-byte file is stored as ~1543 bytes. No
error is raised. A later read returns corrupted data. Because the write looks
successful, this is easy to miss unless you compare bytes, not just object
names and counts.

```
local file:   1500 bytes
stored object: 1543 bytes   <-- +43 bytes of framing, not your data
```

## Cause

Starting in early 2025 the AWS SDKs turned on **default data-integrity
checksums** (CRC32) for every upload, sent as a trailer inside an `aws-chunked`
request body:

- AWS SDK for Go v2, `service/s3` **>= v1.73.0** (2025-01-15)
- boto3 (Python) **>= 1.36** / botocore >= 1.36
- AWS CLI v2 recent builds, and other SDKs on the same schedule

AWS S3 understands `aws-chunked` and strips the framing. Several S3-compatible
stores do not, and persist the chunk-size headers, the chunk terminator, and
the trailing `x-amz-checksum-*` header as part of the object body. OBS is one of
them. The same regression has been reported against MinIO, Ceph RADOS Gateway,
Hetzner Object Storage, IBM COS, Cloudflare R2 and others.

The stored body looks like this (the marked lines are framing the store should
have removed):

```
5dc                                <-- chunk size in hex (prepended)
...your real bytes...
0                                  <-- chunk terminator (appended)
x-amz-checksum-crc32:6hT9oA==      <-- trailing checksum header (appended)
```

## Who is affected, and who is not

| Path | Affected? |
|---|---|
| Backend uploads to OBS via AWS SDK (Go v2, boto3, etc.) | **Yes** |
| Migration / sync scripts using a current AWS SDK | **Yes** |
| Mobile client doing a plain HTTP `PUT` to a presigned URL | No (no SDK, no `aws-chunked`) |
| rclone (uses its own S3 implementation) | Usually not, but byte-verify to be sure |

So the exposure is **server-side and tooling uploads that go through an AWS
SDK**. A mobile app that does a normal HTTP PUT against a presigned URL is not
using the SDK upload path and is not affected.

## Fix

Disable the new default checksum behaviour. The simplest fix needs no code
change, just two environment variables, and works for every AWS SDK and the CLI:

```bash
export AWS_REQUEST_CHECKSUM_CALCULATION=when_required
export AWS_RESPONSE_CHECKSUM_VALIDATION=when_required
```

`when_required` keeps the SDK from adding a checksum unless the specific API
call needs one, which removes the `aws-chunked` trailer for normal PUTs.

### Go (aws-sdk-go-v2), in code

```go
cfg, err := config.LoadDefaultConfig(ctx,
    config.WithRequestChecksumCalculation(aws.RequestChecksumCalculationWhenRequired),
    config.WithResponseChecksumValidation(aws.ResponseChecksumValidationWhenRequired),
)
// then s3.NewFromConfig(cfg, func(o *s3.Options){ o.UsePathStyle = false })
```

(Exact symbol names depend on your SDK version. If the code option is awkward to
thread through, the two environment variables above have the same effect.)

### Python (boto3), in code

```python
from botocore.config import Config
client = boto3.client(
    "s3",
    endpoint_url="https://obs.eu-de.otc.t-systems.com",
    region_name="eu-de",
    config=Config(
        signature_version="s3v4",
        s3={"addressing_style": "virtual"},
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    ),
)
```

This is exactly what this tool sets in `bucketsync/adapters/s3_store.py`.

## Verify, do not assume

Always confirm by **bytes**, not by object name or count. A name-and-count check
passes while the body is silently corrupt. After an upload, compare the source
and stored object sizes, and ideally an MD5 of the content:

```python
src = open("file.jpg", "rb").read()
dst = client.get_object(Bucket=b, Key="file.jpg")["Body"].read()
assert len(src) == len(dst) and md5(src) == md5(dst)
```

## Notes

- Pinning an older SDK (Go `service/s3` <= v1.72.x, boto3 <= 1.35.x) also avoids
  it, but disabling the checksum is the cleaner long-term fix.
- `skip_s3_checksum`-style flags that only suppress the checksum *header* are not
  enough on their own. The body still gets wrapped in `aws-chunked`. Use the
  `when_required` settings above.

## References

- Terraform tracking issue (third-party S3 backends): hashicorp/terraform#37130
- MinIO: minio/minio#21611
- Cloudflare R2 `aws-chunked` strip: opentofu/opentofu#1354
