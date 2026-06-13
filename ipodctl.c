/* ipodctl - minimal command-line iPod sync tool built on libgpod.
 *
 * Usage:
 *   ipodctl init <mountpoint> [ipod-name]
 *   ipodctl ls   <mountpoint> [all|artist|album]
 *   ipodctl add  <mountpoint> <file> [key=value ...]
 *   ipodctl rm   <mountpoint> <track-id> | artist|album <name>
 *
 * Recognized keys for `add`: title artist album genre composer
 *   tracklen (ms) tracknr year bitrate (kbps) samplerate (Hz)
 *
 * Build: gcc -o ipodctl ipodctl.c $(pkg-config --cflags --libs libgpod-1.0)
 */

#include <gpod/itdb.h>
#include <glib/gstdio.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

static void die(const char *msg, GError *error)
{
    fprintf(stderr, "ipodctl: %s%s%s\n", msg,
            error ? ": " : "", error ? error->message : "");
    if (error)
        g_error_free(error);
    exit(1);
}

static int cmd_init(int argc, char **argv)
{
    GError *error = NULL;
    const char *mount = argv[0];
    const char *name = argc > 1 ? argv[1] : "iPod";

    /* model number NULL: libgpod reads it from the device SysInfo if
     * present; artwork support may be unavailable without it. */
    if (!itdb_init_ipod(mount, NULL, name, &error))
        die("failed to initialize iPod", error);

    printf("Initialized iPod directory structure at %s\n", mount);
    return 0;
}

static gboolean print_group(gpointer key, gpointer value, gpointer data)
{
    printf("%5d  %s\n", GPOINTER_TO_INT(value), (const char *)key);
    return FALSE;
}

static const char *orunknown(const char *s)
{
    return s && *s ? s : "(unknown)";
}

static gint track_cmp(gconstpointer a, gconstpointer b)
{
    const Itdb_Track *ta = a, *tb = b;
    int c = g_ascii_strcasecmp(orunknown(ta->artist), orunknown(tb->artist));
    if (c)
        return c;
    c = g_ascii_strcasecmp(orunknown(ta->album), orunknown(tb->album));
    if (c)
        return c;
    if (ta->track_nr != tb->track_nr)
        return ta->track_nr - tb->track_nr;
    return g_ascii_strcasecmp(orunknown(ta->title), orunknown(tb->title));
}

static int cmd_ls(int argc, char **argv)
{
    GError *error = NULL;
    const char *field = argc > 1 ? argv[1] : NULL;
    gboolean all = field && !strcmp(field, "all");

    if (field && !all && strcmp(field, "artist") && strcmp(field, "album"))
        die("ls: field must be 'all', 'artist' or 'album'", NULL);

    Itdb_iTunesDB *itdb = itdb_parse(argv[0], &error);
    if (!itdb)
        die("failed to read iTunesDB", error);

    Itdb_Playlist *mpl = itdb_playlist_mpl(itdb);

    if (field && !all) {
        gboolean by_artist = !strcmp(field, "artist");
        GTree *counts = g_tree_new((GCompareFunc)g_ascii_strcasecmp);

        for (GList *l = mpl->members; l; l = l->next) {
            Itdb_Track *t = l->data;
            const char *key = by_artist ? t->artist : t->album;
            if (!key || !*key)
                key = "(unknown)";
            int n = GPOINTER_TO_INT(g_tree_lookup(counts, (gpointer)key));
            g_tree_insert(counts, (gpointer)key, GINT_TO_POINTER(n + 1));
        }

        printf("iPod \"%s\": %d %ss (%u tracks)\n", mpl->name,
               g_tree_nnodes(counts), field, g_list_length(mpl->members));
        g_tree_foreach(counts, print_group, NULL);
        g_tree_destroy(counts);
        itdb_free(itdb);
        return 0;
    }

    GList *sorted = g_list_sort(g_list_copy(mpl->members), track_cmp);

    unsigned nartists = 0, nalbums = 0;
    const char *pa = NULL, *pal = NULL;
    for (GList *l = sorted; l; l = l->next) {
        Itdb_Track *t = l->data;
        const char *a = orunknown(t->artist), *al = orunknown(t->album);
        if (!pa || g_ascii_strcasecmp(a, pa)) {
            nartists++;
            pal = NULL;
        }
        if (!pal || g_ascii_strcasecmp(al, pal))
            nalbums++;
        pa = a;
        pal = al;
    }

    printf("iPod \"%s\": %u tracks, %u artists, %u albums\n",
           mpl->name, g_list_length(mpl->members), nartists, nalbums);

    pa = pal = NULL;
    for (GList *l = sorted; l; l = l->next) {
        Itdb_Track *t = l->data;
        const char *a = orunknown(t->artist), *al = orunknown(t->album);

        if (!pa || g_ascii_strcasecmp(a, pa)) {
            printf("%s\n", a);
            pa = a;
            pal = NULL;
        }
        if (!pal || g_ascii_strcasecmp(al, pal)) {
            if (all) {
                printf("  %s\n", al);
            } else {
                int n = 0;
                for (GList *m = l; m; m = m->next) {
                    Itdb_Track *u = m->data;
                    if (g_ascii_strcasecmp(orunknown(u->artist), a) ||
                        g_ascii_strcasecmp(orunknown(u->album), al))
                        break;
                    n++;
                }
                printf("  %s (%d track%s)\n", al, n, n == 1 ? "" : "s");
            }
            pal = al;
        }
        if (all) {
            char nr[8] = "   ";
            if (t->track_nr)
                snprintf(nr, sizeof(nr), "%2d.", t->track_nr);
            printf("    %6d  %s %-36.36s %2d:%02d\n", t->id, nr,
                   orunknown(t->title),
                   t->tracklen / 60000, (t->tracklen / 1000) % 60);
        }
    }
    g_list_free(sorted);

    itdb_free(itdb);
    return 0;
}

static int cmd_add(int argc, char **argv)
{
    GError *error = NULL;
    const char *mount = argv[0];
    const char *file = argv[1];

    struct stat st;
    if (g_stat(file, &st) != 0)
        die("cannot stat input file", NULL);

    Itdb_iTunesDB *itdb = itdb_parse(mount, &error);
    if (!itdb)
        die("failed to read iTunesDB (run `ipodctl init` first?)", error);

    Itdb_Track *track = itdb_track_new();
    track->size = st.st_size;
    track->mediatype = ITDB_MEDIATYPE_AUDIO;
    track->filetype = g_strdup("MPEG audio file");

    for (int i = 2; i < argc; i++) {
        char *eq = strchr(argv[i], '=');
        if (!eq) {
            fprintf(stderr, "ipodctl: ignoring malformed arg '%s'\n", argv[i]);
            continue;
        }
        *eq = '\0';
        const char *key = argv[i], *val = eq + 1;

        if (!strcmp(key, "title"))
            track->title = g_strdup(val);
        else if (!strcmp(key, "artist"))
            track->artist = g_strdup(val);
        else if (!strcmp(key, "album"))
            track->album = g_strdup(val);
        else if (!strcmp(key, "genre"))
            track->genre = g_strdup(val);
        else if (!strcmp(key, "composer"))
            track->composer = g_strdup(val);
        else if (!strcmp(key, "tracklen"))
            track->tracklen = atoi(val);
        else if (!strcmp(key, "tracknr"))
            track->track_nr = atoi(val);
        else if (!strcmp(key, "year"))
            track->year = atoi(val);
        else if (!strcmp(key, "bitrate"))
            track->bitrate = atoi(val);
        else if (!strcmp(key, "samplerate"))
            track->samplerate = atoi(val);
        else
            fprintf(stderr, "ipodctl: ignoring unknown key '%s'\n", key);
    }

    if (!track->title)
        track->title = g_strdup(file);

    itdb_track_add(itdb, track, -1);
    itdb_playlist_add_track(itdb_playlist_mpl(itdb), track, -1);

    if (!itdb_cp_track_to_ipod(track, file, &error))
        die("failed to copy track to iPod", error);

    if (!itdb_write(itdb, &error))
        die("failed to write iTunesDB", error);

    printf("Added: %s - %s (%s)\n",
           track->artist ? track->artist : "?",
           track->title, track->ipod_path);

    itdb_free(itdb);
    return 0;
}

static int cmd_rm(int argc, char **argv)
{
    GError *error = NULL;
    const char *mount = argv[0];
    const char *field = NULL, *name = NULL;
    int id = 0;

    if (argc >= 3 &&
        (!strcmp(argv[1], "artist") || !strcmp(argv[1], "album"))) {
        field = argv[1];
        name = argv[2];
    } else if (argc == 2) {
        id = atoi(argv[1]);
    } else {
        die("rm: expected <track-id> or artist|album <name>", NULL);
    }

    Itdb_iTunesDB *itdb = itdb_parse(mount, &error);
    if (!itdb)
        die("failed to read iTunesDB", error);

    GList *victims = NULL;
    for (GList *l = itdb->tracks; l; l = l->next) {
        Itdb_Track *t = l->data;
        gboolean match;
        if (field) {
            const char *val = !strcmp(field, "artist") ? t->artist : t->album;
            match = val && !g_ascii_strcasecmp(val, name);
        } else {
            match = (int)t->id == id;
        }
        if (match)
            victims = g_list_prepend(victims, t);
    }
    if (!victims)
        die(field ? "no tracks match that name (see `ipodctl ls`)"
                  : "no track with that id (see `ipodctl ls`)", NULL);
    victims = g_list_reverse(victims);

    int removed = 0;
    for (GList *v = victims; v; v = v->next) {
        Itdb_Track *track = v->data;

        gchar *path = itdb_filename_on_ipod(track);
        if (path) {
            g_unlink(path);
            g_free(path);
        }

        for (GList *l = itdb->playlists; l; l = l->next) {
            Itdb_Playlist *pl = l->data;
            if (itdb_playlist_contains_track(pl, track))
                itdb_playlist_remove_track(pl, track);
        }

        printf("Removed: %s - %s\n",
               track->artist ? track->artist : "?", track->title);
        itdb_track_remove(track);
        removed++;
    }
    g_list_free(victims);

    if (removed > 1)
        printf("Removed %d tracks\n", removed);

    if (!itdb_write(itdb, &error))
        die("failed to write iTunesDB", error);

    itdb_free(itdb);
    return 0;
}

int main(int argc, char **argv)
{
    if (argc >= 3 && !strcmp(argv[1], "init"))
        return cmd_init(argc - 2, argv + 2);
    if (argc >= 3 && !strcmp(argv[1], "ls"))
        return cmd_ls(argc - 2, argv + 2);
    if (argc >= 4 && !strcmp(argv[1], "add"))
        return cmd_add(argc - 2, argv + 2);
    if (argc >= 4 && !strcmp(argv[1], "rm"))
        return cmd_rm(argc - 2, argv + 2);

    fprintf(stderr,
            "usage: ipodctl init <mountpoint> [ipod-name]\n"
            "       ipodctl ls   <mountpoint> [all|artist|album]\n"
            "       ipodctl add  <mountpoint> <file> [key=value ...]\n"
            "       ipodctl rm   <mountpoint> <track-id>\n"
            "       ipodctl rm   <mountpoint> artist|album <name>\n");
    return 2;
}
