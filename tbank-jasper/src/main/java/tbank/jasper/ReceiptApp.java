package tbank.jasper;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import net.sf.jasperreports.engine.DefaultJasperReportsContext;
import net.sf.jasperreports.engine.JREmptyDataSource;
import net.sf.jasperreports.engine.JRParameter;
import net.sf.jasperreports.engine.JasperCompileManager;
import net.sf.jasperreports.engine.JasperFillManager;
import net.sf.jasperreports.engine.JasperPrint;
import net.sf.jasperreports.engine.JasperReport;
import net.sf.jasperreports.engine.export.JRPdfExporter;
import net.sf.jasperreports.export.SimpleExporterInput;
import net.sf.jasperreports.export.SimpleOutputStreamExporterOutput;
import net.sf.jasperreports.export.SimplePdfExporterConfiguration;

import java.awt.Font;
import java.awt.GraphicsEnvironment;
import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import java.util.logging.LogManager;

/**
 * Bank-shaped T-Bank receipt: JasperReports 6.20.3 → OpenPDF 1.3.30.jaspersoft.2.
 * Full TinkoffSans in resources; OpenPDF subsets painted glyphs (Identity-H).
 */
public final class ReceiptApp {
    public static final String CREATOR =
            "JasperReports Library version 6.20.3-415f9428cffdb6805c6f85bbb29ebaf18813a2ab";
    public static final String PRODUCER = "OpenPDF 1.3.30.jaspersoft.2";
    public static final String SUBJECT = "\"/reports/IB/Receipt\"";

    private static final Map<String, String> REPORTS = Map.of(
            "sbp", "/reports/IB/ReceiptSbp.jrxml",
            "phone", "/reports/IB/ReceiptPhone.jrxml",
            "card_tbank", "/reports/IB/ReceiptCard.jrxml",
            "card", "/reports/IB/ReceiptCard.jrxml",
            "nocomm", "/reports/IB/ReceiptNocomm.jrxml"
    );

    private ReceiptApp() {}

    static void registerAwtFonts() {
        GraphicsEnvironment ge = GraphicsEnvironment.getLocalGraphicsEnvironment();
        // ALSRubl extracted from PDF has no name table — AWT cannot load it.
        // OpenPDF still embeds it via fonts.xml.
        String[] paths = {
                "/fonts/TinkoffSans-Regular.ttf",
                "/fonts/TinkoffSans-Medium.ttf",
                "/fonts/ALSRubl.ttf",
        };
        for (String path : paths) {
            try (InputStream in = ReceiptApp.class.getResourceAsStream(path)) {
                if (in == null) {
                    System.err.println("font missing " + path);
                    continue;
                }
                ge.registerFont(Font.createFont(Font.TRUETYPE_FONT, in));
            } catch (Exception ex) {
                System.err.println("font load " + path + ": " + ex);
            }
        }
    }

    public static InputStream resource(String name) {
        String path = name.startsWith("/") ? name : "/" + name;
        InputStream in = ReceiptApp.class.getResourceAsStream(path);
        if (in == null) {
            throw new IllegalStateException("missing resource " + path);
        }
        return in;
    }

    public static void main(String[] args) throws Exception {
        Thread.setDefaultUncaughtExceptionHandler((t, e) -> {
            try {
                Files.writeString(Path.of("jasper-crash.txt"), e.getClass().getName());
            } catch (Exception ignored) {
            }
        });
        try {
            LogManager.getLogManager().reset();
        } catch (Exception ignored) {
        }
        try {
            String channel = "sbp";
            Path inPath = null;
            Path outPath = null;
            for (int i = 0; i < args.length; i++) {
                String a = args[i];
                if ("--channel".equals(a) && i + 1 < args.length) {
                    channel = args[++i];
                } else if ("--in".equals(a) && i + 1 < args.length) {
                    inPath = Path.of(args[++i]);
                } else if ("--out".equals(a) && i + 1 < args.length) {
                    outPath = Path.of(args[++i]);
                } else if (!a.startsWith("-")) {
                    channel = a;
                }
            }
            channel = channel.toLowerCase(Locale.ROOT);
            if (!REPORTS.containsKey(channel)) {
                System.err.println("unknown channel " + channel);
                System.exit(2);
            }
            byte[] raw = inPath != null
                    ? Files.readAllBytes(inPath)
                    : readAll(System.in);
            ObjectMapper mapper = new ObjectMapper();
            Map<String, Object> params = mapper.readValue(
                    raw, new TypeReference<Map<String, Object>>() {});
            byte[] pdf = render(channel, params);
            if (outPath != null) {
                Files.write(outPath, pdf);
            } else {
                System.out.write(pdf);
            }
        } catch (Throwable t) {
            try {
                String s = t.getClass().getName();
                try {
                    s = s + " " + t.getMessage();
                } catch (Exception ignored) {
                }
                Files.writeString(Path.of("jasper-crash.txt"), s);
            } catch (Exception ignored) {
            }
            System.exit(1);
        }
    }

    public static byte[] render(String channel, Map<String, Object> params)
            throws Exception {
        DefaultJasperReportsContext ctx = DefaultJasperReportsContext.getInstance();
        ctx.setProperty("net.sf.jasperreports.awt.ignore.missing.font", "true");
        ctx.setProperty("net.sf.jasperreports.export.pdf.ignore.missing.font", "true");
        registerAwtFonts();
        String jrxml = REPORTS.get(channel.toLowerCase(Locale.ROOT));
        if (jrxml == null) {
            throw new IllegalArgumentException("unknown channel " + channel);
        }
        try (InputStream in = resource(jrxml)) {
            JasperReport report = JasperCompileManager.compileReport(in);
            Map<String, Object> fill = new HashMap<>();
            for (JRParameter p : report.getParameters()) {
                if (p.isSystemDefined()) {
                    continue;
                }
                fill.put(p.getName(), params.getOrDefault(p.getName(), ""));
            }
            fill.putAll(params);
            JasperPrint print = JasperFillManager.fillReport(
                    report, fill, new JREmptyDataSource());
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            JRPdfExporter exporter = new JRPdfExporter();
            exporter.setExporterInput(new SimpleExporterInput(print));
            exporter.setExporterOutput(new SimpleOutputStreamExporterOutput(bos));
            SimplePdfExporterConfiguration cfg = new SimplePdfExporterConfiguration();
            cfg.setMetadataCreator(CREATOR);
            cfg.setMetadataSubject(SUBJECT);
            Object kw = params.get("keywords");
            if (kw instanceof String && !((String) kw).isBlank()) {
                cfg.setMetadataKeywords((String) kw);
            }
            cfg.setEncrypted(Boolean.FALSE);
            exporter.setConfiguration(cfg);
            exporter.exportReport();
            return bos.toByteArray();
        } catch (Throwable t) {
            try {
                String s = t.getClass().getName();
                try {
                    s = s + " " + t.getMessage();
                } catch (Exception ignored) {
                }
                Files.writeString(Path.of("jasper-crash.txt"), s);
            } catch (Exception ignored) {
            }
            if (t instanceof RuntimeException) {
                throw (RuntimeException) t;
            }
            if (t instanceof Error) {
                throw (Error) t;
            }
            throw new RuntimeException(t);
        }
    }

    static String stack(Throwable t) {
        StringBuilder sb = new StringBuilder();
        for (Throwable c = t; c != null && sb.length() < 20000; c = c.getCause()) {
            sb.append(c.getClass().getName()).append(": ").append(c.getMessage()).append('\n');
            for (StackTraceElement el : c.getStackTrace()) {
                sb.append("  at ").append(el).append('\n');
            }
            sb.append('\n');
        }
        return sb.toString();
    }

    static byte[] readAll(InputStream in) throws Exception {
        BufferedInputStream buf = new BufferedInputStream(in);
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        buf.transferTo(bos);
        return bos.toByteArray();
    }
}
