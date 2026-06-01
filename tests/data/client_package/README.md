# How to create test squashfs and patch files

This guide demonstrates how to create test squashfs files and generate a test patch between versions.

## Steps

1. Create two sample directories:

```bash
mkdir dir_v1 dir_v2
```

2. Add test files with different content:

```bash
echo "Hello v1" > dir_v1/test.txt
echo "Hello v2" > dir_v2/test.txt
```

3. Create squashfs archives for both versions:

```bash
mksquashfs dir_v1 v1.squashfs -comp gzip
mksquashfs dir_v2 v2.squashfs -comp gzip
```

4. Generate a patch from v1 to v2 using zstd:

```bash
zstd --patch-from=v1.squashfs v2.squashfs -o v1_v2.patch
```

5. Clean up:

```bash
rm -rf v2.squashfs
rm -rf dir_v1 dir_v2
```
