#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <cmath>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>
#include <vector>

#include <pipewire/pipewire.h>
#include <spa/param/audio/format-utils.h>

namespace {
constexpr double kPi = 3.14159265358979323846;
constexpr unsigned kRate = 48000;
constexpr float kCeiling = 0.89125094f; // -1 dBFS
constexpr std::array<double, 10> kEqFrequencies =
    {31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000};

struct Options {
    bool enabled = true;
    bool pipewire = false;
    std::string profile = "Music";
    std::string firPath;
    std::string sinkName = "nhmc_original";
    std::string targetSink;
    double bassDb = 0;
    double bassHz = 200;
    double trebleDb = 0;
    double clarityDb = 0;
    bool compressor = false;
    bool reverb = false;
    bool widen = false;
    std::array<double, 10> eq{};
};

double number(const char *text, const std::string &key) {
    char *end = nullptr;
    errno = 0;
    double value = std::strtod(text, &end);
    if (errno || end == text || *end || !std::isfinite(value))
        throw std::runtime_error("invalid number for " + key);
    return value;
}

Options parse_options(int argc, char **argv) {
    Options o;
    for (int i = 1; i < argc; ++i) {
        std::string key(argv[i]);
        auto value = [&]() -> const char * {
            if (++i >= argc) throw std::runtime_error("missing value for " + key);
            return argv[i];
        };
        if (key == "--disabled") o.enabled = false;
        else if (key == "--pipewire") o.pipewire = true;
        else if (key == "--sink") o.sinkName = value();
        else if (key == "--target") o.targetSink = value();
        else if (key == "--profile") o.profile = value();
        else if (key == "--fir") o.firPath = value();
        else if (key == "--bass-db") o.bassDb = number(value(), key);
        else if (key == "--bass-hz") o.bassHz = number(value(), key);
        else if (key == "--treble-db") o.trebleDb = number(value(), key);
        else if (key == "--clarity-db") o.clarityDb = number(value(), key);
        else if (key == "--compressor") o.compressor = std::string(value()) == "1";
        else if (key == "--reverb") o.reverb = std::string(value()) == "1";
        else if (key == "--widen") o.widen = std::string(value()) == "1";
        else if (key == "--eq") {
            std::string values(value());
            size_t start = 0;
            for (size_t band = 0; band < o.eq.size(); ++band) {
                size_t comma = values.find(',', start);
                std::string part = values.substr(start, comma == std::string::npos ? comma : comma - start);
                o.eq[band] = number(part.c_str(), key);
                if (band + 1 < o.eq.size() && comma == std::string::npos)
                    throw std::runtime_error("--eq needs ten comma-separated gains");
                start = comma == std::string::npos ? values.size() : comma + 1;
            }
        } else if (key == "--help") {
            std::puts("nhmc-native [--profile NAME] [--fir PATH] [--disabled] < float32-stereo.f32");
            std::exit(0);
        } else {
            throw std::runtime_error("unknown option: " + key);
        }
    }
    if (o.bassDb < -18 || o.bassDb > 18 || o.trebleDb < -18 || o.trebleDb > 18 ||
        o.clarityDb < -18 || o.clarityDb > 18 || o.bassHz < 20 || o.bassHz > 1000)
        throw std::runtime_error("effect setting is outside the supported range");
    for (double gain : o.eq)
        if (gain < -18 || gain > 18) throw std::runtime_error("EQ gain must be between -18 and 18 dB");
    return o;
}

struct Biquad {
    double b0 = 1, b1 = 0, b2 = 0, a1 = 0, a2 = 0;
    double z1 = 0, z2 = 0;

    double run(double x) {
        double y = b0 * x + z1;
        z1 = b1 * x - a1 * y + z2;
        z2 = b2 * x - a2 * y;
        return y;
    }

    void peak(double hz, double gainDb, double q) {
        if (std::abs(gainDb) < 1e-9) return;
        const double A = std::pow(10.0, gainDb / 40.0);
        const double w = 2.0 * kPi * std::min(hz, 0.49 * kRate) / kRate;
        const double alpha = std::sin(w) / (2.0 * q);
        const double c = std::cos(w);
        normalize(1 + alpha * A, -2 * c, 1 - alpha * A,
                  1 + alpha / A, -2 * c, 1 - alpha / A);
    }

    void shelf(double hz, double gainDb, bool low, double slope = 1.0) {
        if (std::abs(gainDb) < 1e-9) return;
        const double A = std::pow(10.0, gainDb / 40.0);
        const double w = 2.0 * kPi * std::min(hz, 0.49 * kRate) / kRate;
        const double c = std::cos(w);
        const double s = std::sin(w);
        const double alpha = s / 2.0 * std::sqrt((A + 1 / A) * (1 / slope - 1) + 2);
        const double beta = 2 * std::sqrt(A) * alpha;
        if (low) {
            normalize(A * ((A + 1) - (A - 1) * c + beta),
                      2 * A * ((A - 1) - (A + 1) * c),
                      A * ((A + 1) - (A - 1) * c - beta),
                      (A + 1) + (A - 1) * c + beta,
                      -2 * ((A - 1) + (A + 1) * c),
                      (A + 1) + (A - 1) * c - beta);
        } else {
            normalize(A * ((A + 1) + (A - 1) * c + beta),
                      -2 * A * ((A - 1) + (A + 1) * c),
                      A * ((A + 1) + (A - 1) * c - beta),
                      (A + 1) - (A - 1) * c + beta,
                      2 * ((A - 1) - (A + 1) * c),
                      (A + 1) - (A - 1) * c - beta);
        }
    }

private:
    void normalize(double nb0, double nb1, double nb2, double na0, double na1, double na2) {
        b0 = nb0 / na0; b1 = nb1 / na0; b2 = nb2 / na0;
        a1 = na1 / na0; a2 = na2 / na0;
    }
};

struct StereoBiquad {
    std::array<Biquad, 2> ch;
    void peak(double hz, double gain, double q) { for (auto &b : ch) b.peak(hz, gain, q); }
    void shelf(double hz, double gain, bool low) { for (auto &b : ch) b.shelf(hz, gain, low); }
    void process(float &l, float &r) { l = static_cast<float>(ch[0].run(l)); r = static_cast<float>(ch[1].run(r)); }
};

class SpeakerFir {
public:
    void load(const std::string &path) {
        if (path.empty()) return;
        std::ifstream f(path, std::ios::binary);
        if (!f) throw std::runtime_error("cannot open speaker tuning data: " + path);
        std::vector<char> bytes((std::istreambuf_iterator<char>(f)), {});
        if (bytes.size() % sizeof(float) != 0) throw std::runtime_error("invalid speaker tuning data");
        const size_t count = bytes.size() / sizeof(float);
        if (count == 0 || count > 16384) throw std::runtime_error("speaker tuning has an invalid tap count");
        taps_.resize(count);
        std::memcpy(taps_.data(), bytes.data(), bytes.size());
        for (float c : taps_) if (!std::isfinite(c)) throw std::runtime_error("speaker tuning contains invalid coefficients");
        std::reverse(taps_.begin(), taps_.end());
        for (auto &h : history_) h.assign(taps_.size() * 2, 0.0f);
    }

    bool active() const { return !taps_.empty(); }

    void process(float &l, float &r) {
        if (!active()) return;
        std::array<float, 2> input{l, r};
        for (size_t c = 0; c < 2; ++c) {
            auto &h = history_[c];
            h[pos_] = input[c];
            h[pos_ + taps_.size()] = input[c];
            const float *samples = h.data() + pos_ + 1;
            double sum = 0;
            #pragma omp simd reduction(+:sum)
            for (size_t k = 0; k < taps_.size(); ++k) sum += static_cast<double>(taps_[k]) * samples[k];
            input[c] = static_cast<float>(sum);
        }
        if (++pos_ == taps_.size()) pos_ = 0;
        l = input[0]; r = input[1];
    }

private:
    std::vector<float> taps_;
    std::array<std::vector<float>, 2> history_;
    size_t pos_ = 0;
};

class NativeNhmc {
public:
    explicit NativeNhmc(const Options &o) : options_(o) {
        fir_.load(o.firPath);
        bass_.shelf(o.bassHz, o.bassDb, true);
        treble_.shelf(8000, o.trebleDb, false);
        clarity_.peak(3000, o.clarityDb, 0.8);
        for (size_t i = 0; i < eq_.size(); ++i) eq_[i].peak(kEqFrequencies[i], o.eq[i], 1.0);
        for (auto &d : reverb_) d.resize(2048, 0.0f);
        if (!o.pipewire) {
            std::fprintf(stderr, "native DSP ready: profile=%s, speaker FIR=%s\n",
                         o.profile.c_str(), fir_.active() ? "loaded" : "off");
            std::fflush(stderr);
        }
    }

    bool firActive() const { return fir_.active(); }

    void process(float &l, float &r) {
        if (!options_.enabled) return;
        fir_.process(l, r);
        bass_.process(l, r);
        for (auto &b : eq_) b.process(l, r);
        clarity_.process(l, r);
        treble_.process(l, r);
        if (options_.widen) {
            const float mid = (l + r) * 0.5f;
            const float side = (l - r) * 0.5f * 1.25f;
            l = mid + side; r = mid - side;
        }
        if (options_.reverb) {
            constexpr std::array<size_t, 4> delays{557, 733, 887, 1021};
            constexpr std::array<float, 4> gains{0.11f, 0.09f, 0.07f, 0.05f};
            for (size_t i = 0; i < delays.size(); ++i) {
                const size_t index = (reverbPos_ + reverb_[i].size() - delays[i]) % reverb_[i].size();
                const float in = i % 2 ? r : l;
                const float echo = reverb_[i][index];
                reverb_[i][reverbPos_] = in + echo * 0.24f;
                if (i % 2) l += gains[i] * echo; else r += gains[i] * echo;
            }
            if (++reverbPos_ == reverb_[0].size()) reverbPos_ = 0;
        }
        if (options_.compressor) compress(l, r);
        limit(l, r);
    }

private:
    void compress(float &l, float &r) {
        const double peak = std::max(std::abs(static_cast<double>(l)), std::abs(static_cast<double>(r)));
        const double db = 20 * std::log10(std::max(peak, 1e-9));
        constexpr double threshold = -18.0;
        const double targetDb = db > threshold ? threshold + (db - threshold) / 2.0 : db;
        const double desired = std::pow(10.0, (targetDb - db) / 20.0);
        const double coefficient = desired < compressorGain_ ? 0.18 : 0.0009;
        compressorGain_ += coefficient * (desired - compressorGain_);
        l = static_cast<float>(l * compressorGain_);
        r = static_cast<float>(r * compressorGain_);
    }

    void limit(float &l, float &r) {
        const float peak = std::max(std::abs(l), std::abs(r));
        if (peak > kCeiling) {
            const float g = kCeiling / peak;
            l *= g; r *= g;
        }
        if (!std::isfinite(l)) l = 0;
        if (!std::isfinite(r)) r = 0;
    }

    const Options &options_;
    SpeakerFir fir_;
    StereoBiquad bass_, treble_, clarity_;
    std::array<StereoBiquad, 10> eq_;
    std::array<std::vector<float>, 4> reverb_;
    size_t reverbPos_ = 0;
    double compressorGain_ = 1.0;
};

struct StereoRing {
    static constexpr size_t kCapacity = 16384;
    static constexpr size_t kMask = kCapacity - 1;
    static constexpr size_t kPrimeFrames = 1024;
    static constexpr size_t kMaxLagFrames = 4096;

    std::array<float, kCapacity * 2> data{};
    std::atomic<size_t> writePos{0};
    std::atomic<size_t> readPos{0};
    bool primed = false;

    void pushProcessed(NativeNhmc &dsp, const float *in, size_t frames) {
        const size_t w = writePos.load(std::memory_order_relaxed);
        const size_t r = readPos.load(std::memory_order_acquire);
        const size_t used = w - r;
        const size_t free = used < kCapacity ? (kCapacity - used) : 0;
        const size_t n = std::min(frames, free);
        for (size_t i = 0; i < n; ++i) {
            float l = in[i * 2];
            float rch = in[i * 2 + 1];
            dsp.process(l, rch);
            const size_t idx = ((w + i) & kMask) * 2;
            data[idx] = l;
            data[idx + 1] = rch;
        }
        writePos.store(w + n, std::memory_order_release);
    }

    void pop(float *out, size_t frames) {
        size_t r = readPos.load(std::memory_order_relaxed);
        const size_t w = writePos.load(std::memory_order_acquire);
        size_t avail = w - r;
        if (!primed) {
            if (avail < kPrimeFrames) {
                std::fill_n(out, frames * 2, 0.0f);
                return;
            }
            primed = true;
        }
        if (avail > kMaxLagFrames) {
            const size_t drop = avail - (kPrimeFrames * 2);
            r += drop;
            avail -= drop;
        }
        if (avail == 0) {
            primed = false;
            std::fill_n(out, frames * 2, 0.0f);
            return;
        }
        const size_t n = std::min(frames, avail);
        for (size_t i = 0; i < n; ++i) {
            const size_t idx = ((r + i) & kMask) * 2;
            out[i * 2] = data[idx];
            out[i * 2 + 1] = data[idx + 1];
        }
        if (n < frames) {
            std::fill_n(out + n * 2, (frames - n) * 2, 0.0f);
            primed = false;
        }
        readPos.store(r + n, std::memory_order_release);
    }
};

struct PipeWireBridge {
    const Options &options;
    NativeNhmc dsp;
    StereoRing ring;
    struct pw_main_loop *loop = nullptr;
    struct pw_context *context = nullptr;
    struct pw_core *core = nullptr;
    struct pw_stream *capture = nullptr;
    struct pw_stream *playback = nullptr;
    struct spa_hook captureListener{};
    struct spa_hook playbackListener{};
    bool captureReady = false;
    bool playbackReady = false;
    bool announcedReady = false;
    bool failed = false;

    explicit PipeWireBridge(const Options &o) : options(o), dsp(o) {}

    void maybeAnnounceReady() {
        if (captureReady && playbackReady && !announcedReady) {
            announcedReady = true;
            std::fprintf(stderr, "native DSP ready: profile=%s, speaker FIR=%s, pipewire=%s\n",
                         options.profile.c_str(), dsp.firActive() ? "loaded" : "off",
                         options.sinkName.c_str());
            std::fflush(stderr);
        }
    }
};

void on_signal(void *data, int) {
    pw_main_loop_quit(static_cast<struct pw_main_loop *>(data));
}

void on_capture_state_changed(void *data, enum pw_stream_state, enum pw_stream_state state,
                              const char *error) {
    auto *b = static_cast<PipeWireBridge *>(data);
    if (state == PW_STREAM_STATE_ERROR) {
        std::fprintf(stderr, "nhmc-native capture error: %s\n", error ? error : "unknown");
        b->failed = true;
        pw_main_loop_quit(b->loop);
    } else if (state == PW_STREAM_STATE_PAUSED || state == PW_STREAM_STATE_STREAMING) {
        b->captureReady = true;
        b->maybeAnnounceReady();
    }
}

void on_playback_state_changed(void *data, enum pw_stream_state, enum pw_stream_state state,
                               const char *error) {
    auto *b = static_cast<PipeWireBridge *>(data);
    if (state == PW_STREAM_STATE_ERROR) {
        std::fprintf(stderr, "nhmc-native playback error: %s\n", error ? error : "unknown");
        b->failed = true;
        pw_main_loop_quit(b->loop);
    } else if (state == PW_STREAM_STATE_PAUSED || state == PW_STREAM_STATE_STREAMING) {
        b->playbackReady = true;
        b->maybeAnnounceReady();
    }
}

void on_capture_process(void *data) {
    auto *b = static_cast<PipeWireBridge *>(data);
    struct pw_buffer *buf = pw_stream_dequeue_buffer(b->capture);
    if (!buf) return;
    struct spa_data *d = &buf->buffer->datas[0];
    if (d->data && d->chunk && d->chunk->size >= sizeof(float) * 2) {
        const auto *in = reinterpret_cast<const float *>(
            static_cast<const uint8_t *>(d->data) + d->chunk->offset);
        const size_t frames = d->chunk->size / (sizeof(float) * 2);
        b->ring.pushProcessed(b->dsp, in, frames);
    }
    pw_stream_queue_buffer(b->capture, buf);
}

void on_playback_process(void *data) {
    auto *b = static_cast<PipeWireBridge *>(data);
    struct pw_buffer *buf = pw_stream_dequeue_buffer(b->playback);
    if (!buf) return;
    struct spa_data *d = &buf->buffer->datas[0];
    if (d->data) {
        const uint32_t maxFrames = d->maxsize / (sizeof(float) * 2);
        uint32_t frames = buf->requested ? static_cast<uint32_t>(buf->requested) : 1024u;
        if (frames == 0 || frames > maxFrames) frames = maxFrames;
        auto *out = static_cast<float *>(d->data);
        b->ring.pop(out, frames);
        if (d->chunk) {
            d->chunk->offset = 0;
            d->chunk->stride = sizeof(float) * 2;
            d->chunk->size = frames * sizeof(float) * 2;
        }
        buf->size = frames;
    }
    pw_stream_queue_buffer(b->playback, buf);
}

int run_pipewire(const Options &options) {
    pw_init(nullptr, nullptr);
    PipeWireBridge bridge(options);
    bridge.loop = pw_main_loop_new(nullptr);
    if (!bridge.loop) throw std::runtime_error("failed to create PipeWire main loop");

    struct pw_loop *pwLoop = pw_main_loop_get_loop(bridge.loop);
    pw_loop_add_signal(pwLoop, SIGINT, on_signal, bridge.loop);
    pw_loop_add_signal(pwLoop, SIGTERM, on_signal, bridge.loop);

    bridge.context = pw_context_new(pwLoop, nullptr, 0);
    if (!bridge.context) {
        pw_main_loop_destroy(bridge.loop);
        throw std::runtime_error("failed to create PipeWire context");
    }
    bridge.core = pw_context_connect(bridge.context, nullptr, 0);
    if (!bridge.core) {
        pw_context_destroy(bridge.context);
        pw_main_loop_destroy(bridge.loop);
        throw std::runtime_error("failed to connect to PipeWire");
    }

    static const struct pw_stream_events captureEvents = {
        .version = PW_VERSION_STREAM_EVENTS,
        .state_changed = on_capture_state_changed,
        .process = on_capture_process,
    };
    static const struct pw_stream_events playbackEvents = {
        .version = PW_VERSION_STREAM_EVENTS,
        .state_changed = on_playback_state_changed,
        .process = on_playback_process,
    };

    struct pw_properties *capProps = pw_properties_new(
        PW_KEY_MEDIA_CLASS, "Audio/Sink",
        PW_KEY_NODE_NAME, options.sinkName.c_str(),
        PW_KEY_NODE_DESCRIPTION, "NHMC Native Engine",
        PW_KEY_DEVICE_DESCRIPTION, "NHMC Native Engine",
        PW_KEY_NODE_GROUP, options.sinkName.c_str(),
        PW_KEY_NODE_LINK_GROUP, options.sinkName.c_str(),
        PW_KEY_NODE_VIRTUAL, "true",
        PW_KEY_NODE_LATENCY, "1024/48000",
        "node.dont-move", "true",
        "node.dont-reconnect", "true",
        "node.dont-fallback", "true",
        nullptr);
    bridge.capture = pw_stream_new(bridge.core, "NHMC Native Engine", capProps);
    if (!bridge.capture) {
        pw_core_disconnect(bridge.core);
        pw_context_destroy(bridge.context);
        pw_main_loop_destroy(bridge.loop);
        throw std::runtime_error("failed to create PipeWire sink stream");
    }
    pw_stream_add_listener(bridge.capture, &bridge.captureListener, &captureEvents, &bridge);

    struct pw_properties *playProps = pw_properties_new(
        PW_KEY_MEDIA_CLASS, "Stream/Output/Audio",
        PW_KEY_MEDIA_ROLE, "DSP",
        PW_KEY_NODE_NAME, "nhmc_playback",
        PW_KEY_APP_NAME, "NHMC-native-playback",
        PW_KEY_MEDIA_NAME, "NHMC-Native",
        PW_KEY_NODE_GROUP, options.sinkName.c_str(),
        PW_KEY_NODE_LINK_GROUP, options.sinkName.c_str(),
        PW_KEY_NODE_PASSIVE, "true",
        PW_KEY_NODE_LATENCY, "1024/48000",
        "node.dont-move", "true",
        "node.dont-reconnect", "true",
        "node.dont-fallback", "true",
        nullptr);
    if (!options.targetSink.empty()) {
        pw_properties_set(playProps, PW_KEY_TARGET_OBJECT, options.targetSink.c_str());
    }
    bridge.playback = pw_stream_new(bridge.core, "NHMC-Native", playProps);
    if (!bridge.playback) {
        pw_stream_destroy(bridge.capture);
        pw_core_disconnect(bridge.core);
        pw_context_destroy(bridge.context);
        pw_main_loop_destroy(bridge.loop);
        throw std::runtime_error("failed to create PipeWire playback stream");
    }
    pw_stream_add_listener(bridge.playback, &bridge.playbackListener, &playbackEvents, &bridge);

    uint8_t capPod[1024];
    struct spa_pod_builder capBuilder = SPA_POD_BUILDER_INIT(capPod, sizeof(capPod));
    struct spa_audio_info_raw info{};
    info.format = SPA_AUDIO_FORMAT_F32;
    info.rate = kRate;
    info.channels = 2;
    info.position[0] = SPA_AUDIO_CHANNEL_FL;
    info.position[1] = SPA_AUDIO_CHANNEL_FR;
    const struct spa_pod *capParams[1] = {
        spa_format_audio_raw_build(&capBuilder, SPA_PARAM_EnumFormat, &info)
    };

    uint8_t playPod[1024];
    struct spa_pod_builder playBuilder = SPA_POD_BUILDER_INIT(playPod, sizeof(playPod));
    const struct spa_pod *playParams[1] = {
        spa_format_audio_raw_build(&playBuilder, SPA_PARAM_EnumFormat, &info)
    };

    if (pw_stream_connect(bridge.capture, PW_DIRECTION_INPUT, PW_ID_ANY,
                          static_cast<enum pw_stream_flags>(
                              PW_STREAM_FLAG_AUTOCONNECT |
                              PW_STREAM_FLAG_MAP_BUFFERS |
                              PW_STREAM_FLAG_RT_PROCESS),
                          capParams, 1) < 0 ||
        pw_stream_connect(bridge.playback, PW_DIRECTION_OUTPUT, PW_ID_ANY,
                          static_cast<enum pw_stream_flags>(
                              PW_STREAM_FLAG_AUTOCONNECT |
                              PW_STREAM_FLAG_MAP_BUFFERS |
                              PW_STREAM_FLAG_RT_PROCESS),
                          playParams, 1) < 0) {
        pw_stream_destroy(bridge.playback);
        pw_stream_destroy(bridge.capture);
        pw_core_disconnect(bridge.core);
        pw_context_destroy(bridge.context);
        pw_main_loop_destroy(bridge.loop);
        throw std::runtime_error("failed to connect PipeWire streams");
    }

    pw_main_loop_run(bridge.loop);

    pw_stream_destroy(bridge.playback);
    pw_stream_destroy(bridge.capture);
    pw_core_disconnect(bridge.core);
    pw_context_destroy(bridge.context);
    pw_main_loop_destroy(bridge.loop);
    pw_deinit();
    return bridge.failed ? 1 : 0;
}

int run(const Options &options) {
    if (options.pipewire) return run_pipewire(options);
    NativeNhmc dsp(options);
    constexpr size_t kFrames = 256;
    std::array<float, kFrames * 2> samples{};
    while (true) {
        const size_t n = std::fread(samples.data(), sizeof(float), samples.size(), stdin);
        if (n == 0) return std::ferror(stdin) ? 1 : 0;
        if (n % 2 != 0) throw std::runtime_error("input ended in the middle of a stereo frame");
        for (size_t i = 0; i < n; i += 2) dsp.process(samples[i], samples[i + 1]);
        if (std::fwrite(samples.data(), sizeof(float), n, stdout) != n) return 1;
        std::fflush(stdout);
        if (n < samples.size()) return std::ferror(stdin) ? 1 : 0;
    }
}
} // namespace

int main(int argc, char **argv) {
    try {
        return run(parse_options(argc, argv));
    } catch (const std::exception &e) {
        std::fprintf(stderr, "nhmc-native: %s\n", e.what());
        return 2;
    }
}
