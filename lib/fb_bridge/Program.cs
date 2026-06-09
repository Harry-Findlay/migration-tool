using System;
using System.Collections.Generic;
using System.Text.Json;
using FirebirdSql.Data.FirebirdClient;

var opts = new JsonSerializerOptions { WriteIndented = false };

if (args.Length < 2) { Error("Usage: FbBridge <command> <db_path> [args]"); return 1; }

var command = args[0];
var dbPath  = args[1];

var exePath = System.Diagnostics.Process.GetCurrentProcess().MainModule?.FileName ?? "";
var exeDir  = System.IO.Path.GetDirectoryName(exePath) ?? AppContext.BaseDirectory;

Environment.SetEnvironmentVariable("FIREBIRD", exeDir);
Environment.SetEnvironmentVariable("FIREBIRD_TMP", System.IO.Path.GetTempPath());

var fbClientPath = System.IO.Path.Combine(exeDir, "fbclient.dll");
var connStr      = $"User=SYSDBA;Password=masterkey;Database={dbPath};ServerType=1;Charset=UTF8;Dialect=3;ClientLibrary={fbClientPath};";
// Write operations use Charset=NONE so binary CHAR(16) UUID columns
// are not interpreted as UTF8 multibyte characters (which causes truncation)
var connStrWrite = $"User=SYSDBA;Password=masterkey;Database={dbPath};ServerType=1;Charset=NONE;Dialect=3;ClientLibrary={fbClientPath};";

// Read an arg: if "-" read from stdin, otherwise use as-is
string ReadArg(string a)
{
    if (a == "-") return Console.In.ReadToEnd();
    return a;
}

try
{
    switch (command)
    {
        case "test":
        {
            using var conn = Open(connStr);
            using var cmd  = new FbCommand("SELECT COUNT(*) FROM PATIENT WHERE ISHIDDEN=0", conn);
            var count = Convert.ToInt32(cmd.ExecuteScalar());
            Ok(new { count });
            break;
        }
        case "patients":
        {
            using var conn = Open(connStr);
            using var cmd  = new FbCommand("SELECT UID FROM PATIENT WHERE ISHIDDEN=0", conn);
            using var rdr  = cmd.ExecuteReader();
            var ids = new List<string>();
            while (rdr.Read()) ids.Add(rdr.GetGuid(0).ToString());
            Ok(new { patients = ids });
            break;
        }
        case "patient":
        {
            if (args.Length < 3) throw new Exception("patient needs <uid_hex>");
            var uid = args[2];
            using var conn = Open(connStr);
            using var cmd  = new FbCommand(
                $"SELECT UID,ID,BIRTHDATE,GIVENNAMES,FAMILYNAME,SEX FROM PATIENT WHERE UID=X'{uid}'", conn);
            using var rdr = cmd.ExecuteReader();
            if (!rdr.Read()) throw new Exception($"Patient not found: {uid}");
            Ok(new {
                uid         = rdr.IsDBNull(0) ? null : rdr.GetGuid(0).ToString(),
                id          = rdr.IsDBNull(1) ? null : rdr.GetString(1),
                birth_date  = rdr.IsDBNull(2) ? null : rdr.GetDateTime(2).ToString("yyyy-MM-dd"),
                given_names = rdr.IsDBNull(3) ? null : rdr.GetString(3),
                family_name = rdr.IsDBNull(4) ? null : rdr.GetString(4),
                sex         = rdr.IsDBNull(5) ? null : rdr.GetString(5),
            });
            break;
        }
        case "images":
        {
            if (args.Length < 4) throw new Exception("images needs <uid_hex> <images_dir>");
            var uid          = args[2];
            var imagesDir    = args[3];
            var patientUid   = new Guid(Convert.FromHexString(uid)).ToString();
            var patientFolder = System.IO.Path.Combine(imagesDir, patientUid);
            using var conn = Open(connStr);
            using var cmd  = new FbCommand($@"
                SELECT IMAGE.UID, IMAGE.STUDYUID, IMAGE.SOPINSTANCEUID,
                       IMAGE.IMAGECLASS, IMAGE.COMMENTS, IMAGE.ACQUISITIONDATETIME,
                       STUDY.STUDYINSTANCEUID, STUDY.STUDYDATETIME, STUDY.ACCESSIONNUMBER,
                       STUDY.STUDYDESCRIPTION, STUDY.REFERRINGPHYSICIANSNAME
                FROM IMAGE LEFT JOIN STUDY ON IMAGE.STUDYUID=STUDY.UID
                WHERE IMAGE.ISHIDDEN=0 AND STUDY.PATIENTUID=X'{uid}'", conn);
            using var rdr = cmd.ExecuteReader();
            var rows = new List<object>();
            while (rdr.Read())
            {
                var imgUid   = rdr.IsDBNull(0) ? "" : rdr.GetGuid(0).ToString();
                var studyUid = rdr.IsDBNull(1) ? "" : rdr.GetGuid(1).ToString();
                rows.Add(new {
                    image_uid      = imgUid,
                    study_uid      = studyUid,
                    sop_instance   = rdr.IsDBNull(2) ? null : rdr.GetString(2),
                    image_class    = rdr.IsDBNull(3) ? null : rdr.GetString(3),
                    comments       = rdr.IsDBNull(4) ? null : rdr.GetString(4),
                    acq_datetime   = rdr.IsDBNull(5) ? null : rdr.GetDateTime(5).ToString("o"),
                    study_instance = rdr.IsDBNull(6) ? null : rdr.GetString(6),
                    study_datetime = rdr.IsDBNull(7) ? null : rdr.GetDateTime(7).ToString("o"),
                    accession      = rdr.IsDBNull(8) ? null : rdr.GetString(8),
                    description    = rdr.IsDBNull(9) ? null : rdr.GetString(9),
                    ref_physician  = rdr.IsDBNull(10)? null : rdr.GetString(10),
                    file_path      = System.IO.Path.Combine(patientFolder, imgUid),
                });
            }
            Ok(new { images = rows });
            break;
        }
        case "copy":
        {
            var destPath = System.IO.Path.Combine(System.IO.Path.GetTempPath(),
                $"Institution_bridge_{System.Diagnostics.Process.GetCurrentProcess().Id}.fdb");
            try
            {
                System.IO.File.Copy(dbPath, destPath, overwrite: true);
                Ok(new { dest = destPath });
            }
            catch
            {
                using var src  = System.IO.File.Open(dbPath,
                    System.IO.FileMode.Open,
                    System.IO.FileAccess.Read,
                    System.IO.FileShare.ReadWrite | System.IO.FileShare.Delete);
                using var dst  = System.IO.File.Create(destPath);
                src.CopyTo(dst);
                Ok(new { dest = destPath });
            }
            break;
        }
        case "diag":
        {
            var plugins    = System.IO.Path.Combine(exeDir, "plugins");
            var rootDlls   = System.IO.Directory.Exists(exeDir)
                ? System.IO.Directory.GetFiles(exeDir, "*.dll") : Array.Empty<string>();
            var pluginDlls = System.IO.Directory.Exists(plugins)
                ? System.IO.Directory.GetFiles(plugins, "*.dll") : Array.Empty<string>();
            Ok(new {
                exe_dir         = exeDir,
                firebird_env    = Environment.GetEnvironmentVariable("FIREBIRD"),
                fb_client_path  = fbClientPath,
                fbclient_exists = System.IO.File.Exists(fbClientPath),
                engine13_exists = System.IO.File.Exists(System.IO.Path.Combine(exeDir, "plugins", "engine13.dll")),
                dlls_in_exe_dir = rootDlls,
                dlls_in_plugins = pluginDlls,
            });
            break;
        }
        case "schema":
        {
            using var conn = Open(connStr);
            using var cmd = new FbCommand(@"
                SELECT r.RDB$RELATION_NAME, f.RDB$FIELD_NAME, f.RDB$NULL_FLAG,
                    t.RDB$TYPE_NAME, fi.RDB$FIELD_LENGTH
                FROM RDB$RELATION_FIELDS f
                JOIN RDB$RELATIONS r ON r.RDB$RELATION_NAME = f.RDB$RELATION_NAME
                JOIN RDB$FIELDS fi ON fi.RDB$FIELD_NAME = f.RDB$FIELD_SOURCE
                LEFT JOIN RDB$TYPES t ON t.RDB$TYPE = fi.RDB$FIELD_TYPE
                    AND t.RDB$FIELD_NAME = 'RDB$FIELD_TYPE'
                WHERE r.RDB$SYSTEM_FLAG = 0
                    AND r.RDB$RELATION_NAME IN ('PATIENT','STUDY','IMAGE')
                ORDER BY r.RDB$RELATION_NAME, f.RDB$FIELD_POSITION", conn);
            using var rdr = cmd.ExecuteReader();
            var cols = new List<object>();
            while (rdr.Read())
                cols.Add(new {
                    table    = rdr.GetString(0).Trim(),
                    column   = rdr.GetString(1).Trim(),
                    not_null = !rdr.IsDBNull(2) && rdr.GetInt16(2) == 1,
                    type     = rdr.IsDBNull(3) ? null : rdr.GetString(3).Trim(),
                    length   = rdr.IsDBNull(4) ? 0 : rdr.GetInt32(4),
                });
            Ok(new { schema = cols });
            break;
        }
        case "find_patient":
        {
            if (args.Length < 3) throw new Exception("find_patient needs <source_instance_id>");
            var srcId = args[2];
            using var conn = Open(connStr);
            using var cmd  = new FbCommand(
                "SELECT UID FROM PATIENT WHERE SOURCEINSTANCEID=@sid AND ISHIDDEN=0", conn);
            cmd.Parameters.Add("@sid", FbDbType.VarChar).Value = srcId;
            using var rdr = cmd.ExecuteReader();
            if (rdr.Read() && !rdr.IsDBNull(0))
            {
                var raw = (byte[])rdr.GetValue(0);
                var hex = BitConverter.ToString(raw).Replace("-", "").ToLower();
                var found = $"{hex[..8]}-{hex[8..12]}-{hex[12..16]}-{hex[16..20]}-{hex[20..]}";
                Ok(new { uid = found });
            }
            else
            {
                Ok(new { uid = (string?)null });
            }
            break;
        }
        case "write_patient":
        {
            if (args.Length < 3) throw new Exception("write_patient needs <json>");
            var doc = JsonDocument.Parse(ReadArg(args[2])).RootElement;
            var uid = doc.GetProperty("uid").GetString()!;

            using var conn = Open(connStrWrite);
            var patHex = uid.Replace("-", "");
            using var cmd = new FbCommand(
                $"INSERT INTO PATIENT (UID, GIVENNAMES, FAMILYNAME, BIRTHDATE, SEX, " +
                $"ID, SOURCEINSTANCEID, ISHIDDEN, NAMEMAPPINGPOLICY, SOURCE, WASIMPORTEDBYVDDS) " +
                $"VALUES (" +
                $"CAST(X'{patHex}' AS CHAR(16) CHARACTER SET OCTETS), " +
                $"@given, @family, @birth, @sex, @id, @src_id, 0, 0, 2, 1)", conn);

            cmd.Parameters.Add(new FbParameter("@given",  FbDbType.VarChar, 1020) { Value = (object?)Trunc(GetStr(doc, "given_names"),       1020) ?? DBNull.Value });
            cmd.Parameters.Add(new FbParameter("@family", FbDbType.VarChar, 1020) { Value = (object?)Trunc(GetStr(doc, "family_name"),        1020) ?? DBNull.Value });

            var bdStr = GetStr(doc, "birth_date");
            if (bdStr != null && bdStr.Length > 0)
                cmd.Parameters.Add("@birth", FbDbType.TimeStamp).Value = DateTime.Parse(bdStr);
            else
                cmd.Parameters.Add("@birth", FbDbType.TimeStamp).Value = DBNull.Value;

            var sex = GenderToSex(GetStr(doc, "sex"));
            cmd.Parameters.Add("@sex", FbDbType.Integer).Value = (object?)sex ?? DBNull.Value;
            cmd.Parameters.Add(new FbParameter("@id",     FbDbType.VarChar, 1020) { Value = (object?)Trunc(GetStr(doc, "pms_id"),            1020) ?? DBNull.Value });
            cmd.Parameters.Add(new FbParameter("@src_id", FbDbType.VarChar, 1020) { Value = (object?)Trunc(GetStr(doc, "source_instance_id"), 1020) ?? DBNull.Value });

            cmd.ExecuteNonQuery();
            Ok(new { uid });
            break;
        }
        case "write_study":
        {
            if (args.Length < 3) throw new Exception("write_study needs <json>");
            var doc = JsonDocument.Parse(ReadArg(args[2])).RootElement;
            var uid    = doc.GetProperty("uid").GetString()!;
            var patUid = doc.GetProperty("patient_uid").GetString()!;
            var studyIuid = GetStr(doc, "study_instance_uid") ?? Guid.NewGuid().ToString();

            using var conn    = Open(connStrWrite);
            var studyHex      = uid.Replace("-", "");
            var patUidHex     = patUid.Replace("-", "");
            using var cmd = new FbCommand(
                $"INSERT INTO STUDY (UID, PATIENTUID, STUDYINSTANCEUID, " +
                $"STUDYDATETIME, ACCESSIONNUMBER, STUDYDESCRIPTION, REFERRINGPHYSICIANSNAME) " +
                $"VALUES (" +
                $"CAST(X'{studyHex}' AS CHAR(16) CHARACTER SET OCTETS), " +
                $"CAST(X'{patUidHex}' AS CHAR(16) CHARACTER SET OCTETS), " +
                $"@study_iuid, @dt, @accession, @desc, @physician)", conn);

            cmd.Parameters.Add(new FbParameter("@study_iuid", FbDbType.VarChar, 256)  { Value = (object?)Trunc(studyIuid, 256) ?? DBNull.Value });

            var sdtStr = GetStr(doc, "study_datetime");
            if (sdtStr != null && sdtStr.Length > 0)
                cmd.Parameters.Add("@dt", FbDbType.TimeStamp).Value = DateTime.Parse(sdtStr);
            else
                cmd.Parameters.Add("@dt", FbDbType.TimeStamp).Value = DBNull.Value;

            cmd.Parameters.Add(new FbParameter("@accession", FbDbType.VarChar, 64)   { Value = (object?)Trunc(GetStr(doc, "accession_number"),  64)   ?? DBNull.Value });
            cmd.Parameters.Add(new FbParameter("@desc",      FbDbType.VarChar, 256)  { Value = (object?)Trunc(GetStr(doc, "study_description"),  256)  ?? DBNull.Value });
            cmd.Parameters.Add(new FbParameter("@physician", FbDbType.VarChar, 3072) { Value = (object?)Trunc(GetStr(doc, "referring_physician"), 3072) ?? DBNull.Value });

            cmd.ExecuteNonQuery();
            Ok(new { uid });
            break;
        }
        case "write_image":
        {
            if (args.Length < 3) throw new Exception("write_image needs <json>");
            var rawJson = ReadArg(args[2]);
            Console.Error.WriteLine($"[write_image] received {rawJson.Length} chars: {rawJson[..Math.Min(100, rawJson.Length)]}");
            var doc      = JsonDocument.Parse(rawJson).RootElement;
            var uid      = doc.GetProperty("uid").GetString()!;
            var studyUid = doc.GetProperty("study_uid").GetString()!;
            var imageClass = GetStr(doc, "image_class") ?? "Intra";
            var sopUid   = GetStr(doc, "sop_instance_uid") ?? Guid.NewGuid().ToString();
            var isMovie  = doc.TryGetProperty("is_movie", out var imv) && imv.ValueKind == JsonValueKind.True;

            var adtStr = GetStr(doc, "acquisition_dt");
            var acqDt  = (adtStr != null && adtStr.Length > 0)
                ? DateTime.SpecifyKind(DateTimeOffset.Parse(adtStr).DateTime, DateTimeKind.Unspecified)
                : DateTime.SpecifyKind(DateTime.UtcNow, DateTimeKind.Unspecified);

            using var conn  = Open(connStrWrite);

            // Use fully parameterized INSERT with explicit types
            // Charset=NONE connection means no UTF8 conversion issues
            var uidBytes      = UuidToBytes(uid);
            var studyUidBytes = UuidToBytes(studyUid);
            var isMovieShort  = (short)(isMovie ? 1 : 0);

            using var cmd = new FbCommand(@"INSERT INTO IMAGE
                (UID, STUDYUID, SOPINSTANCEUID, IMAGECLASS,
                 ACQUISITIONDATETIME, ACQUISITIONTECHNOLOGY,
                 ISMOVIE, ISEXTERNAL, ISHIDDEN, SOURCE)
                VALUES (@uid, @study_uid, @sop, @class, @dt, 0, @movie, 0, 0, 2)", conn);

            cmd.Parameters.Add(new FbParameter("@uid",       FbDbType.Binary, 16) { Value = uidBytes });
            cmd.Parameters.Add(new FbParameter("@study_uid", FbDbType.Binary, 16) { Value = studyUidBytes });
            cmd.Parameters.Add(new FbParameter("@sop",       FbDbType.VarChar, 256) { Value = Trunc(sopUid, 256) ?? "" });
            cmd.Parameters.Add(new FbParameter("@class",     FbDbType.VarChar, 256) { Value = Trunc(imageClass, 256) ?? "Intra" });
            cmd.Parameters.Add(new FbParameter("@dt",        FbDbType.TimeStamp) { Value = acqDt });
            cmd.Parameters.Add(new FbParameter("@movie",     FbDbType.SmallInt) { Value = isMovieShort });
            cmd.ExecuteNonQuery();

            var acqType = Trunc(GetStr(doc, "acquisition_type"), 256);
            if (acqType != null)
            {
                using var u1 = new FbCommand(
                    "UPDATE IMAGE SET ACQUISITIONTYPENAME=@v WHERE UID=@uid", conn);
                u1.Parameters.Add(new FbParameter("@v",   FbDbType.VarChar, 256) { Value = acqType });
                u1.Parameters.Add(new FbParameter("@uid", FbDbType.Binary,  16)  { Value = uidBytes });
                u1.ExecuteNonQuery();
            }
            var mediaId = Trunc(GetStr(doc, "source_media_id"), 128);
            if (mediaId != null)
            {
                using var u2 = new FbCommand(
                    "UPDATE IMAGE SET DBSWINID=@v WHERE UID=@uid", conn);
                u2.Parameters.Add(new FbParameter("@v",   FbDbType.VarChar, 128) { Value = mediaId });
                u2.Parameters.Add(new FbParameter("@uid", FbDbType.Binary,  16)  { Value = uidBytes });
                u2.ExecuteNonQuery();
            }

            Ok(new { uid });
            break;
        }
        case "query":
        {
            if (args.Length < 3) throw new Exception("query needs <sql>");
            var raw = ReadArg(args[2]);
            // Accept either a plain SQL string or a {"sql": "..."} JSON envelope
            // (the JSON envelope is used when SQL is piped via stdin to avoid
            //  Windows command-line quoting/length issues)
            string sql;
            if (raw.TrimStart().StartsWith("{"))
            {
                var doc = JsonDocument.Parse(raw).RootElement;
                sql = doc.GetProperty("sql").GetString()!;
            }
            else
            {
                sql = raw;
            }
            using var conn = Open(connStr);
            using var cmd  = new FbCommand(sql, conn);
            using var rdr  = cmd.ExecuteReader();
            var rows = new List<Dictionary<string, object?>>();
            while (rdr.Read())
            {
                var row = new Dictionary<string, object?>();
                for (int i = 0; i < rdr.FieldCount; i++)
                    row[rdr.GetName(i)] = rdr.IsDBNull(i) ? null : rdr.GetValue(i)?.ToString();
                rows.Add(row);
            }
            Ok(new { rows });
            break;
        }
        case "execute":
        {
            if (args.Length < 3) throw new Exception("execute needs <sql>");
            var sql = args[2];
            using var conn = Open(connStr);
            using var cmd  = new FbCommand(sql, conn);
            var affected = cmd.ExecuteNonQuery();
            Ok(new { affected });
            break;
        }
        case "echo":
        {
            // Debug: echo back args[2] (reads from stdin if "-")
            var received = args.Length > 2 ? ReadArg(args[2]) : "(no arg)";
            Ok(new { received, length = received.Length });
            break;
        }
        default:
            throw new Exception($"Unknown command: {command}");
    }
    return 0;
}
catch (Exception ex)
{
    Error(ex.Message);
    return 1;
}

FbConnection Open(string cs) { var c = new FbConnection(cs); c.Open(); return c; }
void Ok(object data)   => Console.WriteLine(JsonSerializer.Serialize(new { ok = true,  data }, opts));
void Error(string msg) => Console.WriteLine(JsonSerializer.Serialize(new { ok = false, error = msg }, opts));

static byte[] UuidToBytes(string uid)
{
    var hex = uid.Replace("-", "");
    var bytes = new byte[16];
    for (int i = 0; i < 16; i++)
        bytes[i] = Convert.ToByte(hex.Substring(i * 2, 2), 16);
    return bytes;
}

static int? GenderToSex(string? gender) => gender?.ToUpper() switch
{
    "MALE"   => 77,
    "FEMALE" => 70,
    "OTHER"  => 79,
    _        => null,
};

static string? GetStr(JsonElement doc, string key)
{
    if (doc.TryGetProperty(key, out var el) && el.ValueKind != JsonValueKind.Null)
        return el.GetString();
    return null;
}

static string? Trunc(string? s, int maxLen)
    => s == null ? null : s.Length <= maxLen ? s : s[..maxLen];