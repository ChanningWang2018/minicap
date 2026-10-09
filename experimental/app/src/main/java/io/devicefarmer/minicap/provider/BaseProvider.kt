/*
 * Copyright (C) 2020 Orange
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package io.devicefarmer.minicap.provider

import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.media.Image
import android.media.ImageReader
import android.net.LocalSocket
import android.os.Handler
import android.os.Looper
import android.util.Size
import io.devicefarmer.minicap.output.DisplayOutput
import io.devicefarmer.minicap.output.MinicapClientOutput
import io.devicefarmer.minicap.SimpleServer
import org.slf4j.LoggerFactory
import java.io.ByteArrayOutputStream
import java.io.OutputStream
import java.io.PrintStream
import java.nio.ByteBuffer

/**
 * Base class to provide images of the screen. Those captures can be setup from SurfaceControl - as
 * it currently is - but could as well comes from MediaProjection API if useful in a future use case.
 * It basically receives screen images, do whatever processing needed (here, encodes in jpeg format)
 * and sends the results to an output (could be a file for screenshot, or a minicap client receiving the
 * jpeg stream)
 */
abstract class BaseProvider(private val displayId: Int, private val targetSize: Size, val rotation: Int, val lazyMode: Boolean = false) : SimpleServer.Listener,
    ImageReader.OnImageAvailableListener {

    companion object {
        val log = LoggerFactory.getLogger(BaseProvider::class.java.simpleName)

        //lower than the usual 3s client receive timeout
        private const val FIRST_FRAME_TIMEOUT_MS = 2000L
        private const val FIRST_FRAME_POLL_MS = 50L

        //conservative default refresh rate of the lazy mode frame cache when no explicit
        //-r was requested: a busy screen must not turn every vsync into a pixel copy
        private const val DEFAULT_LAZY_REFRESH_PERIOD_MS = 100L
    }

    private lateinit var clientOutput: DisplayOutput
    private lateinit var imageReader: ImageReader
    private var previousTimeStamp: Long = 0L
    private var framePeriodMs: Long = 0
    private var bitmap: Bitmap? = null //is used to compress the images

    var quality: Int = 100
    var frameRate: Float = Float.MAX_VALUE
        set(value) {
            this.framePeriodMs = (1000 / value).toLong()
            log.info("framePeriodMs: $framePeriodMs")
            field = value
        }

    abstract fun screenshot(printer: PrintStream)
    abstract fun getScreenSize(): Size

    fun getTargetSize(): Size = if(rotation%2 != 0) Size(targetSize.height, targetSize.width) else targetSize
    fun getImageReader(): ImageReader = imageReader

    fun init(out: DisplayOutput) {
        imageReader = ImageReader.newInstance(
            getTargetSize().width,
            getTargetSize().height,
            PixelFormat.RGBA_8888,
            2
        )
        clientOutput = out
    }

    override fun onConnection(socket: LocalSocket) {
        val minicapOutput = MinicapClientOutput(socket)
        minicapOutput.sendBanner(getScreenSize(),getTargetSize(),rotation)
        clientOutput = minicapOutput
        init(clientOutput)

        if (lazyMode) {
            startLazyModeWorker(minicapOutput)
        }
    }

    /**
     * Latest screen frame cached as a bitmap. In lazy mode screen updates only refresh
     * this cache (no JPEG encoding until a client actually requests a frame). Written by
     * the ImageReader callback thread, read by the lazy mode worker, and never mutated
     * after publication, so no further locking is needed.
     */
    @Volatile
    private var latestFrame: Bitmap? = null

    /**
     * Guards the whole frame pipeline lifecycle: frame callbacks ([onImageAvailable], main
     * looper), listener registration ([setFrameListener], connection thread) and release
     * ([releaseFramePipeline], lazy mode worker) all run under this lock, so a release can
     * never interleave with an in-flight callback or a registration.
     */
    private val framePipelineLock = Any()

    /**
     * Set under [framePipelineLock] before the ImageReader is torn down. Once true, no
     * frame callback is processed anymore. The single connection model never rebuilds the
     * pipeline, so it stays true for the rest of the process life.
     */
    @Volatile
    private var framePipelineReleased = false

    /**
     * True once [releaseFramePipeline] ran: the frame pipeline is down and must not be
     * used anymore (single connection model, it is never rebuilt in this process).
     */
    fun isFramePipelineReleased(): Boolean = framePipelineReleased

    /**
     * Registers the frame listener unless the pipeline was already released by the lazy
     * mode worker (client disconnected right away): registering a listener on a closed
     * ImageReader would throw.
     */
    fun setFrameListener(listener: ImageReader.OnImageAvailableListener, handler: Handler) {
        synchronized(framePipelineLock) {
            if (framePipelineReleased) {
                log.info("frame pipeline already released, not registering the frame listener")
                return
            }
            imageReader.setOnImageAvailableListener(listener, handler)
        }
    }

    /**
     * Tears down the frame consumption path once the lazy mode worker stopped (the client
     * is gone): detaches the frame listener, closes the ImageReader and gives the concrete
     * provider a chance to release its own capture resources through [releaseResources].
     * The process stays alive, it just stops being woken up by screen updates.
     */
    private fun releaseFramePipeline() {
        synchronized(framePipelineLock) {
            if (framePipelineReleased) return
            framePipelineReleased = true
            try {
                //detach the listener first so no new callback gets dispatched anymore, then
                //close the reader. An in-flight callback cannot race with this close since
                //it holds the same lock. The main looper handler is passed explicitly
                //because this runs on the worker thread, which has no looper of its own.
                imageReader.setOnImageAvailableListener(null, Handler(Looper.getMainLooper()))
            } catch (e: Exception) {
                log.warn("lazy mode: could not detach the frame listener", e)
            }
            try {
                imageReader.close()
            } catch (e: Exception) {
                log.warn("lazy mode: could not close the image reader", e)
            }
        }
        log.info("lazy mode: frame pipeline released, screen frames are not consumed anymore")
        releaseResources()
    }

    /**
     * Called on the lazy mode worker thread once the frame pipeline has been released, so
     * the concrete provider can free the capture resources it owns (virtual displays and
     * the like). The default implementation releases nothing.
     */
    protected open fun releaseResources() {}

    /**
     * Serves client frame requests one by one: blocks on the request, encodes the latest
     * cached frame and sends it. Exits cleanly when the client disconnects.
     */
    private fun startLazyModeWorker(output: MinicapClientOutput) {
        if (framePeriodMs > 0) {
            log.info("lazy mode: -r limits the cached frame refresh rate to every ${framePeriodMs}ms, frames are only sent on client request")
        }
        Thread({
            log.info("lazy mode worker started")
            while (true) {
                try {
                    if (!output.requestFrame()) {
                        log.info("lazy mode worker: client disconnected (end of stream)")
                        break
                    }
                    sendLatestFrame(output)
                } catch (e: Exception) {
                    log.error("lazy mode worker stopping", e)
                    break
                }
            }
            releaseFramePipeline()
            log.info("lazy mode worker exited")
        }, "lazy-mode-worker").start()
    }

    private fun sendLatestFrame(output: MinicapClientOutput) {
        val frame = latestFrame ?: awaitFirstFrame()
        if (frame == null) {
            log.warn("lazy mode: no frame available for request, the display produced nothing yet")
            return
        }
        val jpeg = ByteArrayOutputStream()
        frame.compress(Bitmap.CompressFormat.JPEG, quality, jpeg)
        output.sendFrame(jpeg.toByteArray())
    }

    /**
     * On connection the display produces an initial frame even if the screen is static,
     * but it may arrive slightly after the first client request. Wait a bounded amount
     * of time for it before giving up on that request.
     */
    private fun awaitFirstFrame(): Bitmap? {
        log.info("lazy mode: waiting for the first frame from the display")
        val deadline = System.currentTimeMillis() + FIRST_FRAME_TIMEOUT_MS
        while (System.currentTimeMillis() < deadline) {
            Thread.sleep(FIRST_FRAME_POLL_MS)
            latestFrame?.let { return it }
        }
        return null
    }

    override fun onImageAvailable(reader: ImageReader) {
        if (framePipelineReleased) return
        synchronized(framePipelineLock) {
            //the pipeline may have been released while this callback, already dispatched on
            //the main looper, was waiting for the lock
            if (framePipelineReleased) return
            val image = reader.acquireLatestImage()
            val currentTime = System.currentTimeMillis()
            if (image != null) {
                if (lazyMode) {
                    //cache the frame as a bitmap without encoding it, it will be encoded only
                    //if a client requests it. The image is always consumed (and closed below)
                    //otherwise the ImageReader buffer fills up and starves the display.
                    //without an explicit -r (framePeriodMs == 0), apply a conservative default
                    //refresh rate so that the cache is not refreshed on every vsync
                    val refreshPeriodMs =
                        if (framePeriodMs > 0) framePeriodMs else DEFAULT_LAZY_REFRESH_PERIOD_MS
                    if (currentTime - previousTimeStamp > refreshPeriodMs) {
                        previousTimeStamp = currentTime
                        cacheFrame(image)
                    }
                } else {
                    if (currentTime - previousTimeStamp > framePeriodMs) {
                        previousTimeStamp = currentTime
                        encode(image, quality, clientOutput.imageBuffer)
                        clientOutput.send()
                    } else {
                        log.debug("skipping frame ($currentTime/$previousTimeStamp)")
                    }
                }
                image.close()
            } else {
                log.debug("no image available")
            }
        }
    }

    /**
     * Copies the image pixels into the frame cache, no JPEG encoding involved.
     */
    private fun cacheFrame(image: Image) {
        with(image) {
            val planes: Array<Image.Plane> = planes
            val buffer: ByteBuffer = planes[0].buffer
            val pixelStride: Int = planes[0].pixelStride
            val rowStride: Int = planes[0].rowStride
            val rowPadding: Int = rowStride - pixelStride * width
            latestFrame = Bitmap.createBitmap(
                width + rowPadding / pixelStride,
                height,
                Bitmap.Config.ARGB_8888
            ).apply {
                copyPixelsFromBuffer(buffer)
            }.run {
                //the image need to be cropped
                Bitmap.createBitmap(this, 0, 0, getTargetSize().width, getTargetSize().height)
            }
        }
    }

    private fun encode(image: Image, q: Int, out: OutputStream) {
        with(image) {
            val planes: Array<Image.Plane> = planes
            val buffer: ByteBuffer = planes[0].buffer
            val pixelStride: Int = planes[0].pixelStride
            val rowStride: Int = planes[0].rowStride
            val rowPadding: Int = rowStride - pixelStride * width
            // createBitmap can be resources consuming
            bitmap ?: Bitmap.createBitmap(
                width + rowPadding / pixelStride,
                height,
                Bitmap.Config.ARGB_8888
            ).apply {
                copyPixelsFromBuffer(buffer)
            }.run {
                //the image need to be cropped
                Bitmap.createBitmap(this, 0, 0, getTargetSize().width, getTargetSize().height)
            }.apply {
                compress(Bitmap.CompressFormat.JPEG, q, out)
            }
        }
    }
}
