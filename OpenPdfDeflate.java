import java.io.*;
import com.lowagie.text.pdf.PdfStream;

/**
 * OpenPDF flate (Jasper/T-Bank profile).
 *
 * One-shot:  java -cp ... OpenPdfDeflate [level]  &lt; u32be_len + payload
 * Serve:     java -cp ... OpenPdfDeflate --serve
 *   request:  u32be_len + u8_level + payload  (len=0 → exit)
 *   response: u32be_out_len + compressed
 */
public class OpenPdfDeflate {
    static int readU32be(InputStream in) throws IOException {
        int b0 = in.read();
        if (b0 < 0) return -1;
        int b1 = in.read();
        int b2 = in.read();
        int b3 = in.read();
        if ((b1 | b2 | b3) < 0) return -1;
        return (b0 << 24) | (b1 << 16) | (b2 << 8) | b3;
    }

    static void writeU32be(OutputStream out, int n) throws IOException {
        out.write((n >>> 24) & 0xff);
        out.write((n >>> 16) & 0xff);
        out.write((n >>> 8) & 0xff);
        out.write(n & 0xff);
    }

    static byte[] compress(byte[] data, int level) throws Exception {
        PdfStream stream = new PdfStream(data);
        stream.flateCompress(level);
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        stream.writeContent(out);
        return out.toByteArray();
    }

    static void serve() throws Exception {
        InputStream in = System.in;
        OutputStream out = System.out;
        while (true) {
            int len = readU32be(in);
            if (len < 0) return;
            if (len == 0) {
                writeU32be(out, 0);
                out.flush();
                return;
            }
            if (len > 50_000_000) {
                throw new IOException("bad length " + len);
            }
            int level = in.read();
            if (level < 0) return;
            if (level < 1 || level > 9) level = 6;
            byte[] data = in.readNBytes(len);
            if (data.length != len) return;
            byte[] zlib = compress(data, level);
            writeU32be(out, zlib.length);
            out.write(zlib);
            out.flush();
        }
    }

    static void oneshot(String[] args) throws Exception {
        int level = 6;
        if (args.length > 0 && !args[0].startsWith("--")) {
            level = Integer.parseInt(args[0]);
        }
        DataInputStream in = new DataInputStream(System.in);
        int len = in.readInt();
        if (len < 0 || len > 50_000_000) {
            System.err.println("bad length " + len);
            System.exit(2);
        }
        byte[] data = in.readNBytes(len);
        if (data.length != len) {
            System.err.println("short read");
            System.exit(3);
        }
        System.out.write(compress(data, level));
    }

    public static void main(String[] args) throws Exception {
        for (String a : args) {
            if ("--serve".equals(a)) {
                serve();
                return;
            }
        }
        oneshot(args);
    }
}
