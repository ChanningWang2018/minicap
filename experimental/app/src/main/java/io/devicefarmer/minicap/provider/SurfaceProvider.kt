/*
 * Copyright (C) 2020 Orange
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *        http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package io.devicefarmer.minicap.provider

import android.graphics.Rect
import android.media.ImageReader
import android.net.LocalSocket
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import android.util.Size
import io.devicefarmer.minicap.output.ScreenshotOutput
import io.devicefarmer.minicap.utils.DisplayInfo
import io.devicefarmer.minicap.utils.DisplayManagerGlobal
import io.devicefarmer.minicap.utils.SurfaceControl
import java.io.PrintStream
import kotlin.system.exitProcess
import android.view.Surface
import android.hardware.display.VirtualDisplay
import io.devicefarmer.minicap.utils.DisplayManager
/**
 * Provides screen images using [SurfaceControl]. This is pretty similar to the native version
 * of minicap but here it is done at a higher level making things a bit easier.
 */
class SurfaceProvider(
    displayId: Int,
    targetSize: Size,
    orientation: Int,
    lazyMode: Boolean = false,
    fitProjection: Boolean = false
) : BaseProvider(displayId, targetSize, orientation, lazyMode, fitProjection) {
    constructor(display: Int) : this(display, currentScreenSize(), currentRotation(), false)
    constructor(display: Int, lazyMode: Boolean) : this(display, currentScreenSize(), currentRotation(), lazyMode)
    private var virtualDisplay: VirtualDisplay? = null
    private var displayManager: DisplayManager? = null
    private var m_displayId : Int = 0;
    companion object {
        private fun currentScreenSize(): Size {
            return currentDisplayInfo().run {
                Size(this.size.width, this.size.height)
            }
        }

        private fun currentRotation(): Int = currentDisplayInfo().rotation

        private fun currentDisplayInfo(): DisplayInfo {
            return DisplayManagerGlobal.getDisplayInfo(0)
        }
    }

    private val handler: Handler = Handler(Looper.getMainLooper())
    private var display: IBinder? = null

    val displayInfo: DisplayInfo = DisplayManagerGlobal.getDisplayInfo(displayId)

    override fun getScreenSize(): Size = displayInfo.size


    override fun screenshot(printer: PrintStream) {
        init(ScreenshotOutput(printer))
        initSurface {
            super.onImageAvailable(it)
            exitProcess(0)
        }
    }

    /**
     *
     */
    override fun onConnection(socket: LocalSocket) {
        super.onConnection(socket)
        initSurface()
    }

    /**
     * Setup the Surface between the display and an ImageReader so that we can grab the
     * screen.
     */
    private fun initSurface(l: ImageReader.OnImageAvailableListener) {
        if (isFramePipelineReleased()) {
            log.info("frame pipeline already released, not setting up the capture display")
            return
        }
        //must be done on the main thread
        // Support  Android 12 (preview),and resolve black screen problem
        try {
            val secure = Build.VERSION.SDK_INT < Build.VERSION_CODES.R || Build.VERSION.SDK_INT == Build.VERSION_CODES.R && "S" != Build.VERSION.CODENAME
            display = SurfaceControl.createDisplay("minicap", secure)
            //initialise the surface to get the display in the ImageReader
            SurfaceControl.openTransaction()
            SurfaceControl.setDisplaySurface(display, getImageReader().surface)
            SurfaceControl.setDisplayProjection(display, 0, Rect(0, 0, getScreenSize().width, getScreenSize().height), Rect(0, 0, captureSize.width, captureSize.height)           )
            SurfaceControl.setDisplayLayerStack(display, displayInfo.layerStack)
        } catch (e: NoSuchMethodException) {
            System.err.println("Handle the exception gracefully")
            System.err.println("NoSuchMethodException Method not found: ${e.message}")
            System.err.println("Try Display: using DisplayManager API")
            try {
                //Varun Kumar:- Android 15 Support
                if (displayManager == null) {
                    displayManager = DisplayManager.create();
                }
                virtualDisplay = displayManager!!.createVirtualDisplay("minicap", getScreenSize().width, getScreenSize().height, m_displayId, getImageReader().surface);
                virtualDisplay!!.surface =getImageReader().surface;
            } catch (displayManagerException : Exception) {
                System.err.println("$displayManagerException Could not create display using DisplayManager")
            }
        } catch (e: IllegalStateException) {
            //the lazy mode worker released the frame pipeline while this setup was running
            //(client connected and disconnected right away): there is nothing left to feed
            log.info("frame pipeline released during display setup, aborting the capture setup")
        }
        finally {
                SurfaceControl.closeTransaction()
        }
        setFrameListener(l, handler)
    }

    private fun initSurface() {
        initSurface(this)
    }

    /**
     * Releases what initSurface created, called on the lazy mode worker thread once the
     * frame pipeline is down: the fallback VirtualDisplay from the DisplayManager API
     * and/or the display created through SurfaceControl, which exposes no destroyDisplay
     * wrapper, so the same reflection pattern as utils/SurfaceControl is applied here.
     */
    override fun releaseResources() {
        try {
            virtualDisplay?.let { vd ->
                log.info("releasing the virtual display")
                vd.release()
                virtualDisplay = null
            }
        } catch (e: Exception) {
            log.warn("could not release the virtual display", e)
        }
        try {
            display?.let { token ->
                log.info("destroying the display created through SurfaceControl")
                Class.forName("android.view.SurfaceControl")
                    .getMethod("destroyDisplay", IBinder::class.java)
                    .invoke(null, token)
                display = null
            }
        } catch (e: Exception) {
            log.warn("could not destroy the display created through SurfaceControl", e)
        }
    }
}
