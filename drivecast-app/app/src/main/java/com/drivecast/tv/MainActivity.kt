package com.drivecast.tv

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.core.splashscreen.SplashScreen.Companion.installSplashScreen
import androidx.compose.animation.core.FastOutLinearInEasing
import androidx.compose.animation.core.LinearOutSlowInEasing
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.scaleIn
import androidx.compose.animation.scaleOut
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.lifecycle.lifecycleScope
import androidx.media3.common.util.UnstableApi
import androidx.navigation.NavHostController
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import com.drivecast.tv.ui.awake.KeepAwakeHost
import com.drivecast.tv.ui.detail.DetailScreen
import com.drivecast.tv.ui.home.HomeScreen
import com.drivecast.tv.ui.player.PlayerScreen
import com.drivecast.tv.ui.settings.SettingsScreen
import com.drivecast.tv.ui.setup.SetupScreen
import com.drivecast.tv.ui.theme.DrivecastTheme
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {
    private var startDest by mutableStateOf<String?>(null)

    @UnstableApi
    override fun onCreate(savedInstanceState: Bundle?) {
        val splash = installSplashScreen()
        super.onCreate(savedInstanceState)
        splash.setKeepOnScreenCondition { startDest == null }

        val container = (application as DrivecastApp).container
        lifecycleScope.launch {
            val cfg = container.configStore.config.first()
            startDest = if (cfg.isConfigured) {
                container.repository.configure(cfg.baseUrl!!, cfg.token!!)
                "home"
            } else {
                "setup"
            }
        }

        setContent {
            CompositionLocalProvider(LocalAppContainer provides container) {
                DrivecastTheme {
                    val start = startDest
                    if (start != null) {
                        DrivecastRoot(startDestination = start)
                    }
                }
            }
        }
    }
}

@UnstableApi
@Composable
private fun DrivecastRoot(startDestination: String) {
    val navController = rememberNavController()
    Box(Modifier.fillMaxSize()) {
        DrivecastNav(navController, startDestination)
        // Global "Are you still watching?" overlay, above every screen.
        KeepAwakeHost(
            onRequestExitPlayer = {
                val route = navController.currentDestination?.route
                if (route != null && route.startsWith("player/")) {
                    navController.popBackStack()
                }
            },
        )
    }
}

@UnstableApi
@Composable
private fun DrivecastNav(navController: NavHostController, startDestination: String) {
    NavHost(
        navController = navController,
        startDestination = startDestination,
        enterTransition = {
            fadeIn(tween(210, easing = LinearOutSlowInEasing)) +
                scaleIn(
                    initialScale = 0.96f,
                    animationSpec = tween(210, easing = LinearOutSlowInEasing),
                )
        },
        exitTransition = {
            fadeOut(tween(90, easing = FastOutLinearInEasing))
        },
        popEnterTransition = {
            fadeIn(tween(210, easing = LinearOutSlowInEasing)) +
                scaleIn(
                    initialScale = 0.96f,
                    animationSpec = tween(210, easing = LinearOutSlowInEasing),
                )
        },
        popExitTransition = {
            fadeOut(tween(90, easing = FastOutLinearInEasing)) +
                scaleOut(targetScale = 0.96f, animationSpec = tween(210))
        },
    ) {
        composable("setup") {
            SetupScreen(onPaired = {
                navController.navigate("home") {
                    popUpTo("setup") { inclusive = true }
                }
            })
        }
        composable("home") {
            HomeScreen(
                onOpenTitle = { titleId -> navController.navigate("detail/$titleId") },
                onPlay = { titleId, fileId -> navController.navigate("player/$titleId/$fileId/false/false/0") },
                onOpenSettings = { navController.navigate("settings") },
            )
        }
        composable("settings") {
            SettingsScreen(
                onExit = { navController.popBackStack() },
                // Home is the entry directly below Settings on the back stack (Settings is only
                // ever pushed from Home), so this is always Home's own NavBackStackEntry — the
                // same one HomeScreen reads via LocalViewModelStoreOwner. Stashing the flag here
                // (rather than only in onExit) means it lands even if the eventual "Save" happens
                // well before the user backs out of the screen.
                onTabsChanged = {
                    navController.previousBackStackEntry?.savedStateHandle?.set("tabsChanged", true)
                },
            )
        }
        composable(
            route = "detail/{titleId}",
            arguments = listOf(navArgument("titleId") { type = NavType.StringType }),
        ) { entry ->
            val titleId = entry.arguments?.getString("titleId").orEmpty()
            DetailScreen(
                titleId = titleId,
                onPlay = { t, f, over, sh, sd -> navController.navigate("player/$t/$f/$over/$sh/$sd") },
            )
        }
        composable(
            route = "player/{titleId}/{fileId}/{startOver}/{shuffle}/{seed}",
            arguments = listOf(
                navArgument("titleId") { type = NavType.StringType },
                navArgument("fileId") { type = NavType.StringType },
                navArgument("startOver") { type = NavType.BoolType },
                navArgument("shuffle") { type = NavType.BoolType },
                navArgument("seed") { type = NavType.LongType },
            ),
        ) { entry ->
            val titleId = entry.arguments?.getString("titleId").orEmpty()
            val fileId = entry.arguments?.getString("fileId").orEmpty()
            val startOver = entry.arguments?.getBoolean("startOver") ?: false
            val shuffle = entry.arguments?.getBoolean("shuffle") ?: false
            val seed = entry.arguments?.getLong("seed") ?: 0L
            PlayerScreen(
                titleId = titleId,
                fileId = fileId,
                startOver = startOver,
                shuffle = shuffle,
                seed = seed,
                onExit = { navController.popBackStack() },
            )
        }
    }
}
