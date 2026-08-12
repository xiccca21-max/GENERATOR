import java.io.*;
import java.nio.file.*;
import java.util.zip.Deflater;

/**
 * Canonical zlib recompression matching Oracle BI Publisher / OpenPDF:
 *   new Deflater(6, false)
 *
 * Usage:
 *   java CanonicalDeflater [--level N] <decoded.bin>     -> zlib bytes to stdout
 *   java CanonicalDeflater [--level N] --stdin           -> decoded from stdin
 *   java CanonicalDeflater [--level N] --match <actual.bin> <decoded.bin>
 *   java CanonicalDeflater --serve
 *       length-prefixed loop (fast for many compressions):
 *         req:  u32be len | u8 level | payload[len]
 *         resp: u32be len | zlib[len]
 *         len=0 closes the worker
 */
public final class CanonicalDeflater {
    private static final int DEFAULT_LEVEL = 6;

    private CanonicalDeflater() {}

    static byte[] compress(byte[] decoded, int level) {
        Deflater deflater = new Deflater(level, false);
        deflater.setInput(decoded);
        deflater.finish();
        byte[] buf = new byte[Math.max(4096, decoded.length + 64)];
        ByteArrayOutputStream bos = new ByteArrayOutputStream(buf.length);
        while (!deflater.finished()) {
            int n = deflater.deflate(buf);
            if (n > 0) {
                bos.write(buf, 0, n);
            }
        }
        deflater.end();
        return bos.toByteArray();
    }

    static byte[] readAll(InputStream in) throws IOException {
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        byte[] tmp = new byte[8192];
        int r;
        while ((r = in.read(tmp)) >= 0) {
            bos.write(tmp, 0, r);
        }
        return bos.toByteArray();
    }

    static int readU32be(InputStream in) throws IOException {
        int b0 = in.read();
        int b1 = in.read();
        int b2 = in.read();
        int b3 = in.read();
        if ((b0 | b1 | b2 | b3) < 0) {
            return -1;
        }
        return (b0 << 24) | (b1 << 16) | (b2 << 8) | b3;
    }

    static void writeU32be(OutputStream out, int n) throws IOException {
        out.write((n >>> 24) & 0xff);
        out.write((n >>> 16) & 0xff);
        out.write((n >>> 8) & 0xff);
        out.write(n & 0xff);
    }

    static void serve() throws IOException {
        InputStream in = System.in;
        OutputStream out = System.out;
        while (true) {
            int len = readU32be(in);
            if (len < 0) {
                return;
            }
            if (len == 0) {
                writeU32be(out, 0);
                out.flush();
                return;
            }
            if (len > 64 * 1024 * 1024) {
                throw new IOException("payload too large: " + len);
            }
            int level = in.read();
            if (level < 0) {
                return;
            }
            if (level < 1 || level > 9) {
                level = DEFAULT_LEVEL;
            }
            byte[] decoded = in.readNBytes(len);
            if (decoded.length != len) {
                return;
            }
            byte[] zlib = compress(decoded, level);
            writeU32be(out, zlib.length);
            out.write(zlib);
            out.flush();
        }
    }

    static void printDiff(byte[] actual, byte[] expected) {
        int lim = Math.min(actual.length, expected.length);
        for (int i = 0; i < lim; i++) {
            if (actual[i] != expected[i]) {
                System.out.printf(
                    "DIFF %d %02x %02x%n",
                    i, actual[i] & 0xff, expected[i] & 0xff
                );
                System.out.printf(
                    "LEN actual=%d expected=%d%n",
                    actual.length, expected.length
                );
                return;
            }
        }
        System.out.printf(
            "DIFF %d LEN actual=%d expected=%d%n",
            lim, actual.length, expected.length
        );
    }

    static int parseLevel(String[] args, int[] consumed) {
        for (int i = 0; i < args.length - 1; i++) {
            if ("--level".equals(args[i])) {
                consumed[0] = i + 2;
                return Integer.parseInt(args[i + 1]);
            }
        }
        consumed[0] = 0;
        return DEFAULT_LEVEL;
    }

    public static void main(String[] args) throws Exception {
        if (args.length == 0) {
            System.err.println("usage: CanonicalDeflater [--level N] [--stdin|--match|--serve] ...");
            System.exit(2);
        }
        for (String a : args) {
            if ("--serve".equals(a)) {
                serve();
                return;
            }
        }
        int[] skip = new int[1];
        int level = parseLevel(args, skip);
        String[] rest = new String[args.length - skip[0]];
        System.arraycopy(args, skip[0], rest, 0, rest.length);
        if (rest.length == 0) {
            System.err.println("usage: CanonicalDeflater [--level N] [--stdin|--match|--serve] ...");
            System.exit(2);
        }

        if ("--match".equals(rest[0])) {
            if (rest.length < 3) {
                System.err.println("usage: CanonicalDeflater [--level N] --match <actual.bin> <decoded.bin>");
                System.exit(2);
            }
            byte[] actual = Files.readAllBytes(Paths.get(rest[1]));
            byte[] decoded = Files.readAllBytes(Paths.get(rest[2]));
            byte[] expected = compress(decoded, level);
            if (actual.length == expected.length) {
                for (int i = 0; i < actual.length; i++) {
                    if (actual[i] != expected[i]) {
                        printDiff(actual, expected);
                        System.exit(1);
                    }
                }
                System.out.println("MATCH");
                System.exit(0);
            }
            printDiff(actual, expected);
            System.exit(1);
        }

        byte[] decoded;
        if ("--stdin".equals(rest[0])) {
            decoded = readAll(System.in);
        } else {
            decoded = Files.readAllBytes(Paths.get(rest[0]));
        }
        byte[] out = compress(decoded, level);
        System.out.write(out);
    }
}
